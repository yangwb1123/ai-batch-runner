"""Regression contracts for TUI runtime wiring boundaries."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from pbatch.models import Task, TaskResult
from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_adapter_legacy import LegacyAdapter
from pbatch.tui_backend import TUIBackend
from pbatch.tui_backend_structured import _new_adapter
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_projection_ui import TUIProjectionMixin
from pbatch.tui_protocol import RuntimeEvent, TurnState


class _SwitchBackend:
    """Small backend supporting both Legacy and Structured attachment."""

    def __init__(self, emit, **options):
        self.emit = emit
        self.agent = options.get("agent") or "pi"
        self.model = options.get("model", "")
        self.provider = options.get("provider", "")
        self.memory_mode = options.get("memory_mode", "off")
        self.session_id = "switch-session"

    def set_emit(self, emit):
        self.emit = emit

    def set_runtime_event_sink(self, sink):
        self.runtime_event_sink = sink

    def new_session(self, _name=""):
        return {"session_id": self.session_id, "session_name": "switch"}

    def run_turn(self, prompt):
        return {"success": True, "stdout": prompt}

    def run_turn_structured(self, prompt):
        return {"success": True, "stdout": prompt}

    def cancel(self):
        return False

    def snapshot(self):
        return {"last_turn_workspace": {"changed": False}}

    def workspace_status(self, _refresh=True):
        return {"available": False, "root": "/tmp"}

    def workspace_diff(self, _path=""):
        return {}


class _PreflightBackend(_SwitchBackend):
    def __init__(self):
        super().__init__(lambda _kind, _text: None)
        self.preflight = threading.Event()
        self.release = threading.Event()
        self.run_calls = 0
        self.cancel_calls = 0

    def workspace_status(self, _refresh=True):
        self.preflight.set()
        self.release.wait(2)
        return {"available": False, "root": "/tmp"}

    def run_turn(self, prompt):
        self.run_calls += 1
        return TaskResult(Task(prompt=prompt), True)

    def cancel(self):
        self.cancel_calls += 1
        return False


class _LongRawBackend(_SwitchBackend):
    def __init__(self, emit, **options):
        super().__init__(emit, **options)
        self.agent = "codex"
        self.runtime_event_sink = None

    def run_turn_structured(self, _prompt):
        events = [(1, "turn.started", {})]
        for seq in range(2, 42):
            events.append((seq, "message.delta", {"text": "x" * 2048}))
        events.extend([
            (42, "message.delta", {"text": "FINAL_TAIL"}),
            (43, "turn.completed", {}),
        ])
        for seq, kind, payload in events:
            self.runtime_event_sink(_event(seq, kind, payload))
        return {"success": True, "stdout": "", "stderr": ""}


class _CompletedRawBackend(_LongRawBackend):
    def run_turn_structured(self, _prompt):
        for seq, kind, payload in (
                (1, "turn.started", {}),
                (2, "message.delta", {"text": "partial"}),
                (3, "message.completed", {"text": "complete"}),
                (4, "turn.completed", {})):
            self.runtime_event_sink(_event(seq, kind, payload))
        return {"success": True, "stdout": "complete", "stderr": ""}


class _AcceptedCancelRaceBackend(_SwitchBackend):
    def __init__(self):
        super().__init__(lambda _kind, _text: None)
        self.run_started = threading.Event()
        self.release_run = threading.Event()
        self.cancel_started = threading.Event()
        self.release_cancel = threading.Event()

    def run_turn(self, prompt):
        self.run_started.set()
        self.release_run.wait(2)
        return TaskResult(Task(prompt=prompt), False, reason="cancelled")

    def cancel(self):
        self.cancel_started.set()
        self.release_run.set()
        self.release_cancel.wait(2)
        return True


class _RetryCancelBackend(_SwitchBackend):
    def __init__(self):
        super().__init__(lambda _kind, _text: None)
        self.run_started = threading.Event()
        self.release_run = threading.Event()
        self.cancel_calls = 0

    def run_turn(self, prompt):
        self.run_started.set()
        self.release_run.wait(2)
        return TaskResult(Task(prompt=prompt), False, reason="cancelled")

    def cancel(self):
        self.cancel_calls += 1
        if self.cancel_calls == 1:
            raise RuntimeError("cancel hook failed")
        self.release_run.set()
        return True


class _MultiCompletedRawBackend(_LongRawBackend):
    def run_turn_structured(self, _prompt):
        events = (
            (1, "turn.started", {}),
            (2, "message.started", {"item_id": "m1"}),
            (3, "message.delta", {"item_id": "m1", "text": "draft"}),
            (4, "message.completed", {"item_id": "m1", "text": "first"}),
            (5, "message.started", {"item_id": "m2"}),
            (6, "message.completed", {"item_id": "m2", "text": "second"}),
            (7, "turn.completed", {}),
        )
        for seq, kind, payload in events:
            self.runtime_event_sink(_event(seq, kind, payload))
        return {"success": True, "stdout": "first\n\nsecond", "stderr": ""}


class _JournalRawBackend(_CompletedRawBackend):
    # Exercise terminal persistence; the separate long-output fixtures cover
    # rendering pressure without making this journal check depend on UI timing.
    def __init__(self, emit, **options):
        super().__init__(emit, **options)
        self.cwd = str(options["cwd"])
        self.session_id = "ses_wiring"


def _event(seq: int, kind: str, payload: dict) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=f"evt_wiring_{seq}", seq=seq,
        occurred_at="2026-08-23T12:00:00Z", workspace_id="ws_wiring",
        session_id="ses_wiring", run_id="run_wiring", turn_id="turn_wiring",
        actor_id="agent_main", kind=kind, payload=payload)


async def _wait_until(predicate, pilot, attempts: int = 600) -> None:
    for _index in range(attempts):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition did not become true")


def _run(coro) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(
                *pending, return_exceptions=True))
        loop.close()


def test_legacy_prestart_interrupt_never_invokes_backend_turn():
    backend = _PreflightBackend()
    adapter = LegacyAdapter(backend)
    session = adapter.start_session({"workspace_id": "ws-preflight"})
    handle = adapter.start_turn({"prompt": "must not run",
                                 "session_id": session.session_id})
    assert backend.preflight.wait(1)

    adapter.interrupt_turn({"turn_id": handle.turn_id})
    backend.release.set()
    assert handle.wait(2)
    adapter.close()

    assert backend.cancel_calls == 0
    assert backend.run_calls == 0
    assert handle.state == TurnState.INTERRUPTED


def test_legacy_post_commit_rejected_cancel_keeps_truthful_result():
    backend = _PreflightBackend()
    backend.release.set()
    adapter = LegacyAdapter(backend)
    session = adapter.start_session({"workspace_id": "ws-commit"})
    committed = threading.Event()
    release = threading.Event()
    commit = adapter._commit_start

    def commit_then_pause(handle):
        commit(handle)
        committed.set()
        release.wait(2)

    adapter._commit_start = commit_then_pause
    handle = adapter.start_turn({
        "prompt": "committed work", "session_id": session.session_id})
    assert committed.wait(1)

    adapter.interrupt_turn({"turn_id": handle.turn_id})
    release.set()
    assert handle.wait(2)
    adapter.close()
    events = list(adapter.events())

    assert backend.run_calls == 1 and backend.cancel_calls == 1
    assert handle.state == TurnState.SUCCEEDED
    assert events[-1].kind == "turn.completed"
    assert any(event.kind == "runtime.warning" and "did not accept" in str(
        event.payload.get("text", "")) for event in events)


def test_legacy_worker_waits_for_accepted_cancel_decision():
    backend = _AcceptedCancelRaceBackend()
    adapter = LegacyAdapter(backend)
    session = adapter.start_session({"workspace_id": "ws-cancel-race"})
    decision_check_entered = threading.Event()
    decision_check_returned = threading.Event()
    check_cancelled = adapter._raise_if_cancelled

    def observed_check(handle):
        decision_check_entered.set()
        check_cancelled(handle)
        decision_check_returned.set()

    adapter._raise_if_cancelled = observed_check
    handle = adapter.start_turn({
        "prompt": "cancel race", "session_id": session.session_id})
    assert backend.run_started.wait(1)
    errors = []

    def request_cancel():
        try:
            adapter.interrupt_turn({"turn_id": handle.turn_id})
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    requester = threading.Thread(target=request_cancel)
    requester.start()
    assert backend.cancel_started.wait(1)
    assert decision_check_entered.wait(1)
    assert not decision_check_returned.wait(0.05)
    backend.release_cancel.set()
    requester.join(2)
    assert handle.wait(2)
    adapter.close()
    events = list(adapter.events())

    assert not errors and not requester.is_alive()
    assert handle.state == TurnState.INTERRUPTED
    assert [event.kind for event in events if event.kind in {
        "turn.completed", "turn.failed", "turn.interrupted",
    }] == ["turn.interrupted"]


def test_legacy_cancel_hook_exception_can_be_retried():
    backend = _RetryCancelBackend()
    adapter = LegacyAdapter(backend)
    session = adapter.start_session({"workspace_id": "ws-cancel-retry"})
    handle = adapter.start_turn({
        "prompt": "retry cancellation", "session_id": session.session_id})
    assert backend.run_started.wait(1)

    with pytest.raises(RuntimeError, match="cancel hook failed"):
        adapter.interrupt_turn({"turn_id": handle.turn_id})
    adapter.interrupt_turn({"turn_id": handle.turn_id})
    assert handle.wait(2)
    adapter.close()

    assert backend.cancel_calls == 2
    assert handle.state == TurnState.INTERRUPTED


def test_structured_default_adapter_uses_selected_agent_binary(tmp_path):
    backend = SimpleNamespace(
        cwd=str(tmp_path), agent="/opt/company/codex", model="gpt-test",
        provider="")
    adapter = _new_adapter(None, backend)
    try:
        assert adapter._argv_prefix == (
            "/opt/company/codex", "exec", "--approve-for-me",
            "--model", "gpt-test", "--json")
    finally:
        adapter.close()


def test_switch_to_codex_with_stale_provider_is_rejected_without_rebind():
    async def scenario():
        app = PiBatchTUI(
            TuiOptions(agent="pi"), backend_factory=_SwitchBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            app._set_option("provider", "stale-provider")
            app._set_option("agent", "codex")
            await pilot.pause()

            assert app.options.agent == "pi"
            assert app._backend.agent == "pi"
            assert not app._structured_backend and app._adapter is not None
            error = "\n".join(
                entry.text for entry in app._entries if entry.role == "error").lower()
            assert "provider" in error
            assert any(word in error for word in ("clear", "remove", "unset"))

    _run(scenario())


def test_startup_codex_provider_is_blocked_until_explicitly_cleared():
    async def scenario():
        app = PiBatchTUI(
            TuiOptions(agent="codex", provider="stale-provider"),
            backend_factory=_SwitchBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            assert app._runtime_blocked_reason
            assert not app._structured_backend
            app._start_turn("must stay blocked")
            assert not app._busy

            app._set_option("provider", "")
            await pilot.pause()
            assert not app._runtime_blocked_reason
            assert app._structured_backend

    _run(scenario())


def test_cross_runtime_rebind_resets_backend_session_to_start(tmp_path):
    async def scenario():
        backend = TUIBackend(
            cwd=str(tmp_path), agent="codex", memory_mode="off")
        backend._session_started = True
        backend.provider_thread_id = "codex-thread"
        app = PiBatchTUI(TuiOptions(agent="codex"), backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            app._set_option("agent", "pi")
            await pilot.pause()

            assert backend.provider_thread_id == ""
            turn, flags_kind = backend._begin_turn()
            assert turn == 1 and flags_kind == "start"
            backend._finish_turn(backend._cancelled_result(backend._task("probe")))
            backend._end_turn()

    _run(scenario())


def test_equal_stderr_and_reason_render_once():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=_SwitchBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            app._finish_turn_result({
                "success": False, "stderr": "single failure",
                "reason": "single failure",
            })
            await pilot.pause()

            body = "".join(entry.text for entry in app._entries)
            assert body.count("single failure") == 1

    _run(scenario())


def test_long_raw_message_keeps_final_tail_in_snapshot_and_tui():
    async def scenario():
        app = PiBatchTUI(
            TuiOptions(agent="codex"), backend_factory=_LongRawBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            app._start_turn("stream a long response")
            await _wait_until(lambda: not app._busy, pilot)

            assert "FINAL_TAIL" in app._runtime_view.assistant_text
            assert "FINAL_TAIL" in app.query_one("#conversation").source

    _run(scenario())


def test_completed_message_replaces_partial_text_in_snapshot_and_tui():
    async def scenario():
        app = PiBatchTUI(
            TuiOptions(agent="codex"), backend_factory=_CompletedRawBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            app._start_turn("replace response")
            await _wait_until(lambda: not app._busy, pilot)

            source = app.query_one("#conversation").source
            assert app._runtime_view.assistant_text == "complete"
            assistant = [entry.text for entry in app._entries
                         if entry.role == "assistant"]
            assert assistant[-1] == "complete"
            assert "complete" in source
            assert "completecomplete" not in source
            assert "partiale" not in source and "partial" not in source

    _run(scenario())


def test_multiple_completed_message_items_survive_tui_replacement():
    async def scenario():
        app = PiBatchTUI(
            TuiOptions(agent="codex"), backend_factory=_MultiCompletedRawBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            app._start_turn("render two messages")
            await _wait_until(lambda: not app._busy, pilot)

            assert app._runtime_view.assistant_text == "first\n\nsecond"
            assistant = [entry.text for entry in app._entries
                         if entry.role == "assistant"]
            assert assistant[-1] == "first\n\nsecond"

    _run(scenario())


def test_real_tui_structured_path_persists_terminal_projection(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime-data"))

    async def scenario():
        app = PiBatchTUI(
            TuiOptions(agent="codex", cwd=str(tmp_path)),
            backend_factory=_JournalRawBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            app._start_turn("persist this turn")
            await _wait_until(lambda: not app._busy, pilot)
            assert app._runtime_view.turn_state == TurnState.SUCCEEDED

    _run(scenario())
    recovery = RuntimeEventJournal(
        str(tmp_path), "ses_wiring").recover()
    assert recovery.events[-1].kind == "turn.completed"
    assert not recovery.degraded


def test_runtime_stream_abort_waits_for_handle_completion():
    done = threading.Event()
    waiting = threading.Event()
    calls = []

    class Handle:
        turn_id = "turn-active"

        def wait(self, timeout=None):
            waiting.set()
            return done.wait(timeout)

    class Adapter:
        def interrupt_turn(self, request):
            calls.append(request)

    result = []
    worker = threading.Thread(target=lambda: result.append(
        TUIProjectionMixin()._abort_runtime_stream(
            Adapter(), Handle(), RuntimeError("stream failed"))))
    worker.start()
    assert waiting.wait(1)
    assert worker.is_alive() and result == []

    done.set()
    worker.join(1)
    assert not worker.is_alive()
    assert calls == [{"turn_id": "turn-active"}]
    assert isinstance(result[0], RuntimeError)


def test_runtime_stream_rejects_event_from_another_handle():
    handle = SimpleNamespace(
        session_id="ses_expected", run_id="run_expected",
        turn_id="turn_expected")

    with pytest.raises(RuntimeError, match="active handle"):
        TUIProjectionMixin._runtime_event_context(
            handle, _event(1, "turn.started", {}))


def test_callback_compatibility_can_replace_a_nonprefix_completion():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=_SwitchBackend)
        async with app.run_test(size=(100, 30)):
            app._disable_runtime_projection()
            app._apply_backend_emit("stdout", "draft")
            app._apply_backend_emit("stdout-replace", "first")

            assistant = [entry.text for entry in app._entries
                         if entry.role == "assistant"]
            assert assistant[-1] == "first"
            app._apply_backend_emit("stdout-replace", "")
            assistant = [entry.text for entry in app._entries
                         if entry.role == "assistant"]
            assert assistant[-1] == ""

    _run(scenario())
