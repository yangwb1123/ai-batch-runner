"""Real UI dispatch survives idle journal failures without losing events."""

from __future__ import annotations

import asyncio
import errno
import os
import time

import pytest
from textual.widgets import Input

from pbatch.tui import PiBatchTUI, TuiOptions
import pbatch.tui_journal as journal_module
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_projection_ui import _IDLE_DRAIN_LIMIT, _IDLE_PENDING_LIMIT


class _Backend:
    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.session_id = "provider-initial"
        self.agent = "test-provider"
        self.prompts = []
        self.new_sessions = []

    def set_emit(self, emit):
        self.emit = emit

    def new_session(self, name=""):
        self.new_sessions.append(name)
        self.session_id = f"provider-{len(self.new_sessions)}"
        return {"session_id": self.session_id, "session_name": name}

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        return {"success": True, "stdout": "completed", "stderr": ""}

    def cancel(self):
        return True

    def workspace_status(self):
        return {"available": False, "root": self.cwd}

    def workspace_diff(self):
        return {}


@pytest.fixture
def journal_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _app(workspace):
    backend = _Backend(workspace)
    app = PiBatchTUI(TuiOptions(cwd=str(workspace), runtime="legacy"), backend=backend)
    return app, backend


def _idle_event(app, text):
    adapter = app._adapter
    adapter._publish("runtime.warning", session=adapter._session,
                     run_id="idle-run", turn_id="", payload={"text": text})
    return adapter._queue._items[-1]


async def _submit(app, pilot, text):
    app.query_one("#composer", Input).value = text
    await pilot.press("enter")
    await pilot.pause()


async def _wait_for_turn(app, pilot):
    deadline = time.monotonic() + 5
    while app._busy and time.monotonic() < deadline:
        await pilot.pause(0.01)
    assert not app._busy


def _fault(patch, mode, *, after=1):
    original_write = journal_module._write_once
    original_temp = journal_module.tempfile.mkstemp
    attempts = []

    def fail_write(descriptor, value):
        attempts.append(value)
        if len(attempts) < after:
            return original_write(descriptor, value)
        if mode == "partial":
            return os.write(descriptor, value[:len(value) // 2])
        raise OSError(errno.ENOSPC, "No space left on device")

    def fail_allocation(*args, **kwargs):
        if str(kwargs.get("prefix", "")).startswith("append-intent.json"):
            attempts.append(kwargs)
            raise PermissionError(errno.EACCES, "Permission denied")
        return original_temp(*args, **kwargs)

    if mode == "permission":
        patch.setattr(journal_module.tempfile, "mkstemp", fail_allocation)
    else:
        patch.setattr(journal_module, "_write_once", fail_write)
    return attempts


def _pending_ids(app):
    return [event.event_id for event in app._pending_idle_runtime_events]


def _assert_refused(app, backend):
    assert app._exception is None
    assert not app._busy and not app._review_busy
    assert backend.prompts == []
    assert app._worker is None
    assert any("retry" in entry.text and "journal" in entry.text.lower()
               for entry in app._entries if entry.role == "error")


@pytest.mark.parametrize("mode", ["enospc", "permission", "partial"])
def test_enter_preserves_failed_batch_and_prompt_for_explicit_retry(
        journal_workspace, monkeypatch, mode):
    async def scenario():
        app, backend = _app(journal_workspace)
        async with app.run_test() as pilot:
            journal = app._event_controller._journal
            original = journal.path.read_bytes()
            event = _idle_event(app, "pending notice")
            with monkeypatch.context() as patch:
                _fault(patch, mode)
                await _submit(app, pilot, "explicit task")
                _assert_refused(app, backend)
                assert app.query_one("#composer", Input).value == "explicit task"
                assert not any(entry.role == "user" for entry in app._entries)
                assert not app._last_task_prompt
                assert not app._runtime_blocked_reason
                assert _pending_ids(app) == [event.event_id]
                assert journal.path.read_bytes() == original
            await pilot.pause()
            assert backend.prompts == []
            await pilot.press("enter")
            await _wait_for_turn(app, pilot)
            assert backend.prompts == ["explicit task"]
            assert _pending_ids(app) == []
            ids = [item.event_id for item in journal.recover().events]
            assert ids.count(event.event_id) == 1
            assert app._runtime_view.turn_state == "SUCCEEDED"
    asyncio.run(scenario())


def test_partial_batch_retry_confirms_each_event_exactly_once(
        journal_workspace, monkeypatch):
    async def scenario():
        app, backend = _app(journal_workspace)
        async with app.run_test() as pilot:
            journal = app._event_controller._journal
            events = [_idle_event(app, f"notice {index}") for index in range(3)]
            with monkeypatch.context() as patch:
                _fault(patch, "partial", after=2)
                await _submit(app, pilot, "must not start")
                _assert_refused(app, backend)
                assert _pending_ids(app) == [event.event_id for event in events[1:]]
                assert events[0].event_id in {
                    item.event_id for item in journal.recover().events}
            await _submit(app, pilot, "/new")
            assert len(backend.new_sessions) == 1
            assert backend.prompts == []
            assert _pending_ids(app) == []
            ids = [item.event_id for item in journal.recover().events]
            assert all(ids.count(event.event_id) == 1 for event in events)
            assert app._journal_session_id != journal.session_id
    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["/new", "/agent replacement", "/review"])
def test_command_failure_keeps_adapter_and_pending_history(
        journal_workspace, monkeypatch, command):
    async def scenario():
        app, backend = _app(journal_workspace)
        async with app.run_test() as pilot:
            adapter = app._adapter
            event = _idle_event(app, "before command")
            if command == "/review":
                monkeypatch.setattr(app, "_appserver_review_mode", lambda: (True, ""))
            with monkeypatch.context() as patch:
                _fault(patch, "enospc")
                await _submit(app, pilot, command)
                _assert_refused(app, backend)
                assert app._adapter is adapter
                assert backend.new_sessions == []
                assert backend.agent == "test-provider"
                assert app.options.agent == ""
                assert _pending_ids(app) == [event.event_id]
            await _submit(app, pilot, "/new")
            assert _pending_ids(app) == []
            assert len(backend.new_sessions) == 1
            assert backend.prompts == []
            recovered = RuntimeEventJournal(
                str(journal_workspace), event.session_id).recover()
            assert sum(item.event_id == event.event_id for item in recovered.events) == 1
    asyncio.run(scenario())


def test_failed_batch_does_not_drain_more_provider_events(
        journal_workspace, monkeypatch):
    async def scenario():
        app, backend = _app(journal_workspace)
        async with app.run_test() as pilot:
            app._adapter._queue.maxsize = _IDLE_PENDING_LIMIT + 20
            for index in range(_IDLE_PENDING_LIMIT + 1):
                _idle_event(app, f"notice {index}")
            with monkeypatch.context() as patch:
                _fault(patch, "enospc")
                await _submit(app, pilot, "must not start")
                retained = _pending_ids(app)
                assert len(retained) == _IDLE_PENDING_LIMIT
                newest = _idle_event(app, "arrived during storage failure")
                await pilot.press("enter")
                await pilot.pause()
                _assert_refused(app, backend)
                assert _pending_ids(app) == retained
                queued = list(app._adapter._queue._items)
                assert len(queued) == 2 and queued[-1].event_id == newest.event_id
    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["/new", "/agent replacement"])
def test_failed_target_session_append_is_retained_until_explicit_new(
        journal_workspace, monkeypatch, command):
    async def scenario():
        app, backend = _app(journal_workspace)
        async with app.run_test() as pilot:
            old_session = app._journal_session_id
            with monkeypatch.context() as patch:
                _fault(patch, "enospc")
                await _submit(app, pilot, command)
                assert app._exception is None and not app._busy
                assert backend.prompts == []
                assert len(backend.new_sessions) == 1
                assert app._journal_session_id == old_session
                assert app._runtime_blocked_reason
                pending = list(app._pending_idle_runtime_events)
                assert len(pending) == 1 and pending[0].session_id != old_session
                assert not any("New session started" in entry.text or
                               "`agent` set to" in entry.text for entry in app._entries)
            await pilot.pause()
            assert len(backend.new_sessions) == 1 and backend.prompts == []
            await _submit(app, pilot, "/new")
            assert not app._runtime_blocked_reason
            assert _pending_ids(app) == []
            assert len(backend.new_sessions) == 2 and backend.prompts == []
            recovered = RuntimeEventJournal(
                str(journal_workspace), pending[0].session_id).recover()
            assert sum(event.event_id == pending[0].event_id
                       for event in recovered.events) == 1
    asyncio.run(scenario())


def test_new_read_only_journal_failure_never_starts_provider(journal_workspace):
    async def scenario():
        app, backend = _app(journal_workspace)
        async with app.run_test() as pilot:
            journal = app._event_controller._journal
            journal.max_bytes = journal.path.stat().st_size
            event = _idle_event(app, "notice crossing configured journal capacity")
            await _submit(app, pilot, "must not start")
            _assert_refused(app, backend)
            assert app._runtime_blocked_reason
            assert app._event_controller.read_only_reason
            assert app._quarantined_runtime_events[-1].event_id == event.event_id
            await _submit(app, pilot, "/new")
            assert not app._runtime_blocked_reason
            assert len(backend.new_sessions) == 1 and backend.prompts == []
    asyncio.run(scenario())


def test_continuous_idle_producer_has_bounded_action_work(journal_workspace, monkeypatch):
    app, backend = _app(journal_workspace)
    delivered = []

    class ContinuousStream:
        def get(self, timeout=0):
            from pbatch.tui_protocol import RuntimeEvent
            number = len(delivered) + 1
            event = RuntimeEvent(
                event_id=f"evt_backlog_{number}", seq=number, occurred_at="now",
                workspace_id="ws_backlog", session_id="ses_backlog",
                run_id="run_backlog", turn_id="", actor_id="agent_main",
                kind="runtime.warning", payload={"text": "notice"})
            delivered.append(event)
            return event

    class Adapter:
        def events(self):
            return ContinuousStream()

    app._adapter = Adapter()
    applied = []
    monkeypatch.setattr(app, "_ingest_runtime_event", applied.append)
    with pytest.raises(RuntimeError, match="backlog.*limit"):
        app._drain_previous_idle_events("ses_backlog")
    assert len(applied) == _IDLE_DRAIN_LIMIT
    assert len(delivered) == _IDLE_DRAIN_LIMIT + _IDLE_PENDING_LIMIT
    assert _pending_ids(app) == [event.event_id for event in delivered[-_IDLE_PENDING_LIMIT:]]
    assert backend.prompts == []
