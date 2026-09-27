"""Durable local submissions never invent a provider start acknowledgement."""

import asyncio
import queue
import threading
import time
import uuid
from dataclasses import replace

import pytest
from textual.widgets import Input

from pbatch import config
from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_adapter_appserver import CodexAppServerAdapter
from pbatch.tui_adapter_pi_rpc import PiRpcAdapter
from pbatch.tui_backend import TUIBackend
from pbatch.tui_controller import TUIEventController
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_protocol import RuntimeCapabilities, RuntimeEvent, TurnState
from pbatch.tui_runtime import RuntimeInfo, RuntimeSession, TurnHandle


_PATHS = [("pi-rpc", False), ("app-server", False), ("app-server", True)]


class _StartRuntime:
    def __init__(self, cwd, mode, behavior="success"):
        self.cwd, self.mode, self.behavior = str(cwd), mode, behavior
        self.entered, self.release, self.closed = (
            threading.Event(), threading.Event(), threading.Event())
        self.queue, self.calls, self.published = queue.Queue(), [], []
        self.interrupts, self.handle = [], None
        self.session = RuntimeSession(
            uuid.uuid4().hex, "provider", self.cwd, "now", "ACTIVE")

    def runtime_info(self):
        name = "pi-rpc" if self.mode == "pi-rpc" else "codex-app-server"
        return RuntimeInfo(name, 1, RuntimeCapabilities(
            runtime=name, capabilities={"review": True, "interrupt": True}))

    def initialize(self, request):
        return self.runtime_info()

    def start_session(self, request):
        return self.session

    def start_turn(self, request):
        self.calls.append(dict(request))
        self.handle = TurnHandle(
            self.session.session_id, request.get("run_id", "unclaimed-run"),
            request.get("turn_id", "unclaimed-turn"), "agent_main", "now")
        self.entered.set()
        if self.behavior == "blocked":
            assert self.closed.wait(5), "start RPC was not closed"
            raise RuntimeError("start RPC closed before acknowledgement")
        if self.behavior == "error":
            assert self.release.wait(5), "test did not release failed start RPC"
            raise RuntimeError("provider start request rejected")
        self._publish_turn()
        return self.handle

    start_review = start_turn

    def _publish_turn(self):
        kinds = ["turn.queued", "turn.started", "turn.completed"]
        if self.behavior in {"warning-first", "queued-after-warning-backward"}:
            kinds.insert(0, "runtime.warning")
        if self.behavior == "no-queued":
            kinds.pop(0)
        if self.behavior == "duplicate-queued":
            kinds.insert(1, "turn.queued")
        if self.behavior == "child-actor":
            kinds.insert(2, "actor.spawned")
        for seq, kind in enumerate(kinds, 1):
            event = self.event(seq, kind)
            if self.behavior == "foreign-queued" and seq == 1:
                event = replace(event, turn_id="foreign-turn")
            if self.behavior == "backward-seq" and kind == "turn.completed":
                event = replace(event, seq=1)
            if self.behavior == "queued-backward-seq" and kind == "turn.queued":
                event = replace(event, seq=10)
            if self.behavior == "foreign-actor-queued" and kind == "turn.queued":
                event = replace(event, actor_id="foreign-actor")
            if self.behavior == "queued-after-warning-backward":
                event = replace(event, seq={"runtime.warning": 10, "turn.queued": 1,
                                           "turn.started": 11, "turn.completed": 12}[kind])
            if kind == "actor.spawned":
                event = replace(event, actor_id="reviewer", payload={
                    "actor_id": "reviewer", "parent_actor_id": "agent_main"})
            self.published.append(event)
            self.queue.put(event)
            if self.behavior == "replayed-queued" and kind == "turn.queued":
                self.queue.put(event)
        if self.behavior == "wrong-handle":
            self.handle = replace(self.handle, run_id="foreign-run")
        self.handle.state = TurnState.SUCCEEDED
        self.handle._done.set()

    def event(self, seq, kind):
        state = {"turn.queued": TurnState.QUEUED,
                 "turn.started": TurnState.RUNNING,
                 "turn.completed": TurnState.SUCCEEDED}.get(kind, "")
        return RuntimeEvent(
            event_id=f"evt_{uuid.uuid4().hex}", seq=seq,
            occurred_at="2026-09-25T00:00:00Z", workspace_id=self.cwd,
            session_id=self.session.session_id, run_id=self.handle.run_id,
            turn_id=self.handle.turn_id, actor_id=self.handle.actor_id,
            kind=kind, payload={"state": state},
            extensions={"runtime": self.runtime_info().runtime})

    def events(self):
        return self.queue

    def interrupt_turn(self, request):
        self.interrupts.append(request)

    def close(self):
        self.closed.set()
        self.release.set()
        self.queue.put(None)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(config, "TUI_JOURNAL_ENABLED", True)
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


def _app(workspace, runtime, *, timeout=5, factory=None):
    factory = factory or (lambda *_a, **_kw: runtime)
    return PiBatchTUI(
        TuiOptions(cwd=str(workspace), agent="pi", runtime=runtime.mode,
                   timeout=timeout, memory_mode="off"),
        backend=TUIBackend(cwd=str(workspace), agent="pi", memory_mode="off"),
        appserver_factory=factory, pi_rpc_factory=factory)


async def _until(predicate, pilot):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        await pilot.pause(0.01)
    assert predicate()


def _launch(app, review):
    if review:
        app._slash_review(())
    else:
        assert app._start_turn("local submission without provider acknowledgement")


def _turn_events(journal):
    return [event for event in journal.recover().events
            if event.kind.startswith("turn.")]


def _assert_unknown(journal, request):
    events = _turn_events(journal)
    assert [event.kind for event in events] == ["turn.queued", "turn.failed"]
    assert events[-1].payload["state"] == TurnState.UNKNOWN
    assert all(event.extensions["runtime"] == "tui-coordinator" for event in events)
    assert all(event.run_id == request["run_id"] and
               event.turn_id == request["turn_id"] for event in events)
    assert events[0].seq < events[1].seq
    recovered = TUIEventController(journal=RuntimeEventJournal(
        events[0].workspace_id, journal.session_id))
    assert recovered.snapshot().turn_state == TurnState.UNKNOWN
    assert not recovered.read_only_reason
    recovered.close()


@pytest.mark.parametrize("mode,review", _PATHS)
@pytest.mark.parametrize("failure", ["timeout", "cancel", "error"])
def test_start_failure_has_durable_identity_without_fake_started(
        workspace, mode, review, failure):
    async def scenario():
        behavior = "error" if failure == "error" else "blocked"
        runtime = _StartRuntime(workspace, mode, behavior)
        app = _app(workspace, runtime, timeout=0.4 if failure == "timeout" else 5)
        async with app.run_test() as pilot:
            _launch(app, review)
            await _until(runtime.entered.is_set, pilot)
            journal, request = app._event_controller._journal, runtime.calls[0]
            before = _turn_events(journal)
            assert len(before) == 1 and before[0].kind == "turn.queued"
            assert request["run_id"] == before[0].run_id
            assert request["turn_id"] == before[0].turn_id
            assert before[0].extensions["runtime"] == "tui-coordinator"
            if failure == "cancel":
                app.action_cancel()
            elif failure == "error":
                runtime.release.set()
            await _until(lambda: not app._busy, pilot)
            assert runtime.closed.is_set() and not runtime.interrupts
            assert not app._review_busy and app._runtime_blocked_reason
            assert app._exception is None
            _assert_unknown(journal, request)
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,review", _PATHS)
@pytest.mark.parametrize("behavior", [
    "success", "no-queued", "replayed-queued", "child-actor", "warning-first"])
def test_success_has_one_queued_and_preserves_provider_order(
        workspace, mode, review, behavior):
    async def scenario():
        runtime = _StartRuntime(workspace, mode, behavior)
        app = _app(workspace, runtime)
        async with app.run_test() as pilot:
            _launch(app, review)
            await _until(lambda: not app._busy, pilot)
            events = _turn_events(app._event_controller._journal)
            assert [event.kind for event in events] == [
                "turn.queued", "turn.started", "turn.completed"]
            assert app._runtime_view.turn_state == TurnState.SUCCEEDED
            assert [event.seq for event in events] == sorted({event.seq for event in events})
            originals = {event.event_id: event for event in runtime.published}
            for event in events[1:]:
                original = originals[event.event_id]
                assert event.extensions["runtime_source_seq"] == original.seq
                assert event.seq == original.seq + 1
                assert "runtime_source_seq" not in original.extensions
            if behavior == "child-actor":
                actors = [event for event in app._event_controller._journal.recover().events
                          if event.kind == "actor.spawned"]
                assert len(actors) == 1 and actors[0].actor_id == "reviewer"
                assert actors[0].extensions["runtime_source_seq"] == 3
            assert not app._runtime_blocked_reason
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,review", _PATHS)
@pytest.mark.parametrize("behavior", [
    "foreign-queued", "duplicate-queued", "backward-seq", "queued-backward-seq", "queued-after-warning-backward",
    "wrong-handle", "foreign-actor-queued"])
def test_local_queued_does_not_relax_provider_identity_or_order(
        workspace, mode, review, behavior):
    async def scenario():
        runtime = _StartRuntime(workspace, mode, behavior)
        app = _app(workspace, runtime)
        async with app.run_test() as pilot:
            _launch(app, review)
            await _until(lambda: not app._busy, pilot)
            events = _turn_events(app._event_controller._journal)
            assert app._runtime_view.turn_state == TurnState.UNKNOWN
            assert runtime.closed.is_set() and app._runtime_blocked_reason
            assert sum(event.kind == "turn.queued" for event in events) == 1
            assert not any(event.kind == "turn.completed" for event in events)
            assert all(event.turn_id != "foreign-turn" for event in events)
            assert all(event.run_id != "foreign-run" for event in events)
            assert events[-1].payload["state"] == TurnState.UNKNOWN
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,review", _PATHS)
def test_start_intent_write_failure_preserves_input_without_provider_call(
        workspace, monkeypatch, mode, review):
    async def scenario():
        runtime = _StartRuntime(workspace, mode)
        app = _app(workspace, runtime)
        async with app.run_test() as pilot:
            journal = app._event_controller._journal
            append = journal.append
            def fail_local_intent(event):
                if event.kind == "turn.queued":
                    raise PermissionError("submission journal permission denied")
                return append(event)
            monkeypatch.setattr(journal, "append", fail_local_intent)
            text = "/review" if review else "keep this unsent prompt"
            app.query_one("#composer", Input).value = text
            await pilot.press("enter")
            assert not app._busy and not app._review_busy
            assert app.query_one("#composer", Input).value == text
            assert runtime.calls == [] and not runtime.closed.is_set()
            assert app._exception is None and _turn_events(journal) == []
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,review", _PATHS)
def test_explicit_new_recovers_after_preack_failure_without_replay(
        workspace, mode, review):
    async def scenario():
        failed = _StartRuntime(workspace, mode, "error")
        fresh = _StartRuntime(workspace, mode)
        available = iter([failed, fresh])
        app = _app(workspace, failed, factory=lambda *_a, **_kw: next(available))
        async with app.run_test() as pilot:
            _launch(app, review)
            await _until(failed.entered.is_set, pilot)
            old_journal = app._event_controller._journal
            failed.release.set()
            await _until(lambda: not app._busy, pilot)
            _assert_unknown(old_journal, failed.calls[0])
            app._new_session()
            assert app._adapter is fresh and not app._runtime_blocked_reason
            assert fresh.calls == [] and len(failed.calls) == 1
            _launch(app, review)
            await _until(lambda: not app._busy, pilot)
            assert app._runtime_view.turn_state == TurnState.SUCCEEDED
            assert len(fresh.calls) == 1 and len(failed.calls) == 1
            assert fresh.calls[0]["run_id"] != failed.calls[0]["run_id"]
            _assert_unknown(old_journal, failed.calls[0])
    asyncio.run(scenario())


@pytest.mark.parametrize("adapter_class", [PiRpcAdapter, CodexAppServerAdapter])
def test_real_adapters_keep_caller_submission_identity(tmp_path, monkeypatch, adapter_class):
    adapter = adapter_class(tmp_path)
    adapter._initialized = True
    adapter.session.provider_session_id = "provider-session"
    monkeypatch.setattr(adapter, "_call", lambda *_a, **_kw: {"turn": {"id": "provider-turn"}})
    request = {"prompt": "test", "session_id": adapter.session.session_id,
               "run_id": "owned-run", "turn_id": "owned-turn", "actor_id": "owned-actor"}
    try:
        handle = adapter.start_turn(request)
        assert (handle.run_id, handle.turn_id, handle.actor_id) == (
            "owned-run", "owned-turn", "owned-actor")
    finally:
        adapter.close()
