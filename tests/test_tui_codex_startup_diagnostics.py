"""Startup diagnostics must not consume the Codex provider-header window.

Owned synthetic streams force stderr before stdout without a provider call,
wall-clock race, or access to account session histories.
"""
from __future__ import annotations

import threading
import time

import pytest

from pbatch.tui_adapter_codex import CodexExecAdapter
from pbatch.tui_backend import TUIBackend
from pbatch.tui_controller import TUIEventController
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_protocol import TurnState
from pbatch.tui_runtime import InitializeRequest, StartTurnRequest
from pbatch.tui_sessions import recover_resume_target
from test_tui_adapter_codex import FakeProc, FakeSpawn, THREAD_ID, _line
from test_tui_codex_fencing import StreamedProc, _drain, _wait_until

WARNING = "failed to refresh available models: timeout waiting for child process to exit"


def _through_warning(adapter):
    events = []
    for _ in range(8):
        event = adapter._queue.get(2)
        assert event is not None
        events.append(event)
        if event.kind == "runtime.warning":
            return events
    raise AssertionError("startup diagnostic was not emitted")


def _through_terminal(adapter):
    deadline = time.monotonic() + 3
    events = []
    for _ in range(64):
        event = adapter._queue.get(max(0, deadline - time.monotonic()))
        assert event is not None
        events.append(event)
        if event.kind == "turn.completed":
            return events
    raise AssertionError("terminal event was not emitted within the bounded stream")


def _reply_lines(provider_id=THREAD_ID, reply="GOT_IT"):
    return [
        _line({"type": "thread.started", "thread_id": provider_id}),
        _line({"type": "turn.started"}),
        _line({"type": "item.completed", "item": {
            "id": "message_a", "type": "agent_message", "text": reply}}),
        _line({"type": "turn.completed", "usage": {"output_tokens": 1}}),
    ]


def _release_reply(process, **kwargs):
    for line in _reply_lines(**kwargs):
        process.lines.push(line)
    process.finish()


@pytest.mark.parametrize("mode", ["normal", "resumed", "forked"])
def test_stderr_leaves_header_open_and_journal_recovers_exact_identity(
        tmp_path, monkeypatch, mode):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    process = StreamedProc(stderr=[WARNING + "\n"])
    adapter = CodexExecAdapter(tmp_path, spawn=FakeSpawn(process))
    adapter.initialize(InitializeRequest(workspace_id="ws_startup"))
    if mode != "normal":
        method = adapter.resume_session if mode == "resumed" else adapter.fork_session
        method({"provider_session_id": "parent_thread"})
    journal = RuntimeEventJournal(str(tmp_path), adapter.session.session_id)
    controller = TUIEventController(journal=journal)
    try:
        handle = adapter.start_turn(StartTurnRequest("remember", run_id="run_a"))
        early = _through_warning(adapter)
        for event in early:
            controller.ingest(event)
        assert handle.state == TurnState.QUEUED
        assert controller.snapshot().turn_state == TurnState.QUEUED
        assert WARNING in controller.snapshot().error_text
        assert not any(event.kind == "session.created" for event in early)
        _release_reply(process)
        assert handle.wait(2)
        adapter.close()
        remaining = list(adapter.events())
        for event in remaining:
            controller.ingest(event)
        events = early + remaining
        sessions = [event for event in events if event.kind == "session.created"]
        assert len(sessions) == 1 and sessions[0].payload["provider_session_id"] == THREAD_ID
        assert adapter.session.provider_session_id == THREAD_ID
        assert sum(event.kind == "turn.queued" for event in events) == 1
        assert sum(event.kind == "turn.started" for event in events) == 1
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        assert controller.snapshot().assistant_text == "GOT_IT"
        assert WARNING in controller.snapshot().error_text
        assert controller.snapshot().turn_state == TurnState.SUCCEEDED
        target = recover_resume_target(journal.recover())
        assert target is not None and target.provider_session_id == THREAD_ID
    finally:
        adapter.close()
        controller.close()


def test_stderr_protocol_text_never_admits_a_header_or_terminal(tmp_path):
    diagnostics = [_line({"type": "thread.started", "thread_id": "forged"}),
                   _line({"type": "turn.completed"}), WARNING + "\n"]
    process = StreamedProc(stderr=diagnostics)
    adapter = CodexExecAdapter(tmp_path, spawn=FakeSpawn(process))
    adapter.initialize()
    try:
        handle = adapter.start_turn("work")
        events = []
        for _ in diagnostics:
            events.extend(_through_warning(adapter))
        assert [event.kind for event in events] == ["turn.queued"] + ["runtime.warning"] * 3
        assert handle.state == TurnState.QUEUED and not handle.done
        assert adapter.session.provider_session_id == ""
        _release_reply(process)
        assert handle.wait(2)
        assert adapter.session.provider_session_id == THREAD_ID
    finally:
        adapter.close()


def test_duplicate_header_after_startup_stderr_is_still_fenced(tmp_path):
    process = StreamedProc(stderr=[WARNING + "\n"])
    adapter = CodexExecAdapter(tmp_path, spawn=FakeSpawn(process))
    adapter.initialize()
    try:
        handle = adapter.start_turn("work")
        _through_warning(adapter)
        process.lines.push(_line({"type": "thread.started", "thread_id": THREAD_ID}))
        # Still QUEUED, but the first real header already consumed the window.
        _release_reply(process, provider_id="must_not_replace")
        assert handle.wait(2)
        assert adapter.session.provider_session_id == THREAD_ID
        adapter.close()
        sessions = [event for event in adapter.events() if event.kind == "session.created"]
        assert len(sessions) == 1 and sessions[0].payload["provider_session_id"] == THREAD_ID
    finally:
        adapter.close()


def test_startup_stderr_pressure_keeps_bounds_and_header_admission(tmp_path):
    process = StreamedProc(stderr=[WARNING + "\n"] * 8)
    adapter = CodexExecAdapter(tmp_path, spawn=FakeSpawn(process), maxsize=2)
    adapter.initialize()
    controller = TUIEventController()
    try:
        handle = adapter.start_turn("work")
        assert _wait_until(lambda: adapter._overflow_reported)
        assert len(adapter._queue._items) <= 2
        assert handle.state == TurnState.QUEUED
        events = _drain(adapter)
        _release_reply(process)
        events.extend(_through_terminal(adapter))
        assert handle.wait(2)
        for event in events:
            controller.ingest(event)
        assert adapter.session.provider_session_id == THREAD_ID
        assert sum(event.kind == "runtime.error" for event in events) == 1
        assert sum(event.kind == "turn.queued" for event in events) == 1
        assert controller.snapshot().turn_state == TurnState.SUCCEEDED
        assert controller.snapshot().assistant_text == "GOT_IT"
    finally:
        adapter.close()
        controller.close()


def test_backend_continues_thread_after_pre_header_diagnostic(tmp_path):
    first = StreamedProc(stderr=[WARNING + "\n"])
    spawn = FakeSpawn(first, FakeProc(_reply_lines(reply="KAPPA_7")))
    backend = TUIBackend(cwd=str(tmp_path), agent="codex", memory_mode="off",
                         session_id="stderr_resume", timeout=5)
    warning_seen = threading.Event()
    adapters, results = [], []

    def factory(cwd):
        adapter = CodexExecAdapter(cwd, spawn=spawn)
        adapters.append(adapter)
        return adapter

    def receive(event):
        if event.kind == "runtime.warning":
            warning_seen.set()

    backend.set_runtime_event_sink(receive)
    worker = threading.Thread(target=lambda: results.append(
        backend.run_turn_structured("remember", adapter_factory=factory)))
    worker.start()
    try:
        assert warning_seen.wait(2)
        _release_reply(first)
        worker.join(3)
        assert not worker.is_alive()
        assert len(results) == 1 and results[0].success
        assert results[0].stdout == "GOT_IT" and WARNING in results[0].stderr
        second = backend.run_turn_structured("recall", adapter_factory=factory)
        assert second.success and second.stdout == "KAPPA_7"
        assert spawn.calls[1][0] == (
            "codex", "exec", "--approve-for-me", "resume", THREAD_ID, "recall", "--json")
        assert backend.provider_thread_id == THREAD_ID
        assert len(adapters) == 2 and adapters[0] is not adapters[1]
    finally:
        first.finish()
        for adapter in adapters:
            adapter.close()
        worker.join(3)
        assert not worker.is_alive()


@pytest.mark.parametrize("returncode, expected", [(0, TurnState.UNKNOWN), (1, TurnState.FAILED)])
def test_stderr_without_protocol_cannot_claim_success(tmp_path, returncode, expected):
    process = StreamedProc(stderr=[WARNING + "\n"])
    adapter = CodexExecAdapter(tmp_path, spawn=FakeSpawn(process))
    adapter.initialize()
    controller = TUIEventController()
    try:
        handle = adapter.start_turn("work")
        events = _through_warning(adapter)
        process.finish(returncode)
        assert handle.wait(2)
        adapter.close()
        events.extend(adapter.events())
        for event in events:
            controller.ingest(event)
        assert handle.state == expected == controller.snapshot().turn_state
        assert WARNING in controller.snapshot().error_text
        assert adapter.session.provider_session_id == ""
        assert sum(event.kind == "turn.queued" for event in events) == 1
        assert sum(event.kind == "turn.started" for event in events) == 1
        assert not any(event.kind == "turn.completed" for event in events)
    finally:
        adapter.close()
        controller.close()


def test_interrupt_after_startup_warning_keeps_header_fenced(tmp_path):
    process = StreamedProc(stderr=[WARNING + "\n"])
    adapter = CodexExecAdapter(tmp_path, spawn=FakeSpawn(process))
    adapter.initialize()
    controller = TUIEventController()
    try:
        handle = adapter.start_turn("work")
        events = _through_warning(adapter)
        adapter.interrupt_turn({"turn_id": handle.turn_id})
        assert handle.wait(2) and handle.state == TurnState.INTERRUPTED
        adapter._dispatch(handle, {"type": "thread.started", "thread_id": "late"}, "")
        adapter.close()
        events.extend(adapter.events())
        for event in events:
            controller.ingest(event)
        assert adapter.session.provider_session_id == ""
        assert sum(event.kind == "turn.queued" for event in events) == 1
        assert sum(event.kind == "turn.started" for event in events) == 1
        assert controller.snapshot().turn_state == TurnState.INTERRUPTED
        assert WARNING in controller.snapshot().error_text
    finally:
        adapter.close()
        controller.close()
