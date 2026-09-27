"""Active journal failures must not interrupt the UI's finish cleanup."""

from __future__ import annotations

import asyncio
import errno
import json
import queue
import threading
import time
import uuid

import pytest
from textual.widgets import Input

from pbatch.tui import PiBatchTUI, TuiOptions
import pbatch.tui_journal as journal_module
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_protocol import RuntimeCapabilities, RuntimeEvent, TurnState
from pbatch.tui_runtime import RuntimeInfo, RuntimeSession, TurnHandle


class _Backend:
    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.session_id = "provider-session"
        self.prompts = []

    def set_emit(self, emit):
        self.emit = emit

    def new_session(self, name=""):
        self.session_id += "-new"
        return {"session_id": self.session_id, "session_name": name}

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        self.emit("stdout", "provider answer")
        return {"success": True, "stdout": "provider answer", "stderr": ""}

    def cancel(self):
        return True

    def workspace_status(self):
        return {"available": False, "root": self.cwd}

    def workspace_diff(self):
        return {}


class _Runtime:
    def __init__(self, cwd):
        self.cwd, self.requests = str(cwd), []
        self.queue, self.closed = queue.Queue(), threading.Event()
        self.session = RuntimeSession(
            f"session-{uuid.uuid4().hex}", "provider", self.cwd, "now", "ACTIVE")

    def runtime_info(self):
        return RuntimeInfo("codex-app-server", 1, RuntimeCapabilities(
            runtime="codex-app-server", capabilities={"review": True}))

    def initialize(self, request):
        return self.runtime_info()

    def start_session(self, request):
        if request.get("session_id"):
            self.session = RuntimeSession(
                request["session_id"], "provider", self.cwd, "now", "ACTIVE")
        return self.session

    def start_turn(self, request):
        self.requests.append(dict(request))
        handle = TurnHandle(
            self.session.session_id, request.get("run_id", "run"),
            request.get("turn_id", "turn"), "agent_main", "now")
        values = [
            ("turn.queued", {"state": TurnState.QUEUED}),
            ("turn.started", {"state": TurnState.RUNNING}),
            ("message.completed", {"item_id": "answer", "text": "provider answer"}),
            ("turn.completed", {"state": TurnState.SUCCEEDED}),
        ]
        for seq, (kind, payload) in enumerate(values, 1):
            self.queue.put(RuntimeEvent(
                event_id=f"evt_{uuid.uuid4().hex}", seq=seq, occurred_at="now",
                workspace_id=self.cwd, session_id=handle.session_id,
                run_id=handle.run_id, turn_id=handle.turn_id, actor_id=handle.actor_id,
                kind=kind, payload=payload))
        handle.state = TurnState.SUCCEEDED
        handle._done.set()
        return handle

    start_review = start_turn

    def events(self):
        return self.queue

    def close(self):
        self.closed.set()


@pytest.fixture
def active_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _fail_started_writes(patch, journal, *, persistent=False):
    original = journal_module._write_once
    failures = []

    def write(descriptor, value):
        kind = json.loads(value)["kind"]
        if kind == "turn.started" or (persistent and failures):
            failures.append(kind)
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(descriptor, value)

    patch.setattr(journal_module, "_write_once", write)
    return failures


def _fail_metadata_after_started(patch, journal):
    original_append, original_meta = journal.append, journal._ensure_meta
    armed, failures = [], []

    def append(event):
        result = original_append(event)
        if event.kind == "turn.started":
            armed.append(True)
        return result

    def ensure_meta():
        if armed:
            failures.append("metadata")
            raise PermissionError(errno.EACCES, "Permission denied", str(journal.meta_path))
        return original_meta()

    patch.setattr(journal, "append", append)
    patch.setattr(journal, "_ensure_meta", ensure_meta)
    return failures


def _make_app(workspace, consumer):
    backend, runtime = _Backend(workspace), _Runtime(workspace)
    mode = "legacy" if consumer == "legacy" else "app-server"
    app = PiBatchTUI(TuiOptions(cwd=str(workspace), runtime=mode, timeout=5),
                     backend=backend, appserver_factory=lambda cwd: runtime)
    return app, backend, runtime


def _assert_finished(app, journal, fault):
    assert app._exception is None
    assert not app._busy and not app._review_busy
    assert app._worker is None and not app._cancel_pending
    assert any(entry.role == "error" for entry in app._entries)
    assert app._last_status != "Ready"
    if fault == "queued-enospc":
        assert app._runtime_view.turn_state == TurnState.UNKNOWN
    else:
        assert "unconfirmed" in app._runtime_blocked_reason
        assert "Unable to confirm the saved failure state" in app._runtime_blocked_reason
        assert "`/new`" in app._runtime_blocked_reason
    records = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert not any(event["kind"] == "turn.completed" for event in records)
    if fault.startswith("queued-"):
        assert not any(event["kind"] == "turn.started" for event in records)


@pytest.mark.parametrize("consumer", ["legacy", "turn", "review"])
@pytest.mark.parametrize("fault", [
    "queued-enospc", "running-permission", "queued-persistent-enospc"])
def test_active_journal_failure_finishes_ui_without_crashing(
        active_workspace, monkeypatch, fault, consumer):
    async def scenario():
        app, backend, runtime = _make_app(active_workspace, consumer)
        async with app.run_test() as pilot:
            journal = app._event_controller._journal
            follow_up = app._follow_up_queue.enqueue(
                "must remain queued", app._adapter_session_id)
            with monkeypatch.context() as patch:
                failures = (_fail_metadata_after_started(patch, journal)
                            if fault == "running-permission" else _fail_started_writes(
                                patch, journal, persistent="persistent" in fault))
                app.query_one("#composer", Input).value = (
                    "/review" if consumer == "review" else "one explicit task")
                await pilot.press("enter")
                deadline = time.monotonic() + 3
                while app._busy and app._exception is None and time.monotonic() < deadline:
                    await pilot.pause(0.01)
                assert failures, "the active-turn journal fault was never reached"
                _assert_finished(app, journal, fault)
                assert len(backend.prompts) + len(runtime.requests) <= 1
                if consumer != "legacy":
                    assert runtime.closed.is_set()
            await pilot.pause()
            assert len(backend.prompts) + len(runtime.requests) <= 1
            assert app._follow_up_queue.peek() == follow_up
    asyncio.run(scenario())


def test_read_only_history_refuses_review_until_explicit_new(active_workspace):
    async def scenario():
        app, backend, runtime = _make_app(active_workspace, "review")
        async with app.run_test() as pilot:
            historical = RuntimeEventJournal(str(active_workspace), "ses_readonly_history")
            historical.append(RuntimeEvent(
                event_id="evt_readonly_history", seq=1, occurred_at="now",
                workspace_id=str(active_workspace), session_id=historical.session_id,
                run_id="", turn_id="", actor_id="agent_main", kind="session.created",
                payload={}, extensions={"runtime": "legacy"}))
            composer = app.query_one("#composer", Input)
            composer.value = f"/resume {historical.session_id}"
            await pilot.press("enter")
            await pilot.pause()
            assert app._journal_session_id == historical.session_id
            assert "Recovered history is read-only" in app._runtime_blocked_reason
            controller = app._event_controller
            composer.value = "/review"
            await pilot.press("enter")
            await pilot.pause()
            assert app._exception is None and not app._busy and not app._review_busy
            assert runtime.requests == [] and backend.prompts == []
            assert composer.value == "/review"
            assert app._event_controller is controller
            assert app._journal_session_id == historical.session_id
            assert "Recovered history is read-only" in app._runtime_blocked_reason
            composer.value = "/new"
            await pilot.press("enter")
            await pilot.pause()
            assert not app._runtime_blocked_reason
            assert app._journal_session_id != historical.session_id
            assert runtime.requests == []
            composer.value = "/review"
            await pilot.press("enter")
            deadline = time.monotonic() + 3
            while app._busy and time.monotonic() < deadline:
                await pilot.pause(0.01)
            assert app._exception is None and not app._busy and not app._review_busy
            assert len(runtime.requests) == 1 and backend.prompts == []
            assert app._runtime_view.turn_state == TurnState.SUCCEEDED
    asyncio.run(scenario())
