"""Fresh Codex exec startup/turn/interrupt fencing regression.

Race cases use owned synthetic spawns and pushed JSONL lines only.  They do
not start a real provider or claim account, region or credential authority.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from pbatch import tui_adapter_codex as adapter_module
from pbatch.tui_adapter_codex import CodexExecAdapter
from pbatch.tui_runtime import InitializeRequest, InterruptRequest, StartTurnRequest
from test_tui_adapter_codex import BlockingProc, FakeProc, FakeSpawn, _line


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class GatedSpawn:
    """Record every spawn call and block inside the call until released."""

    def __init__(self, *processes):
        self.processes = list(processes)
        self.calls = []
        self.release = threading.Event()

    def __call__(self, argv, cwd):
        self.calls.append((tuple(argv), cwd))
        assert self.release.wait(5)
        return self.processes.pop(0)


class PushedLines:
    """Iterable provider stdout that only ends when the owner closes it."""

    def __init__(self):
        self._items: queue.Queue = queue.Queue()
        self._closed = threading.Event()

    def push(self, line):
        self._items.put(line)

    def close(self):
        self._closed.set()

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            try:
                return self._items.get(timeout=0.05)
            except queue.Empty:
                if self._closed.is_set():
                    raise StopIteration
                continue


class StreamedProc(FakeProc):
    """Popen-shaped process whose stdout lines are pushed by the test."""

    def __init__(self, stderr=()):
        super().__init__((), stderr)
        self.lines = PushedLines()
        self.stdout = self.lines
        self.returncode = None
        self._finished = threading.Event()

    def finish(self, returncode=0):
        self.returncode = returncode
        self.lines.close()
        self._finished.set()

    def poll(self):
        return self.returncode if self._finished.is_set() else None

    def wait(self, timeout=None):
        self._finished.wait(timeout)
        return self.returncode

    def kill(self):
        FakeProc.kill(self)
        self.lines.close()
        self._finished.set()

    def terminate(self):
        FakeProc.terminate(self)


def _adapter(tmp_path, *processes, spawn=None):
    adapter = CodexExecAdapter(str(tmp_path), spawn=spawn or FakeSpawn(*processes))
    adapter.initialize(InitializeRequest(workspace_id="ws_test"))
    return adapter


def _drain(adapter):
    events = []
    while True:
        try:
            event = adapter._queue.get(0.02)
        except queue.Empty:
            break
        if event is None:
            break
        events.append(event)
    return events


def test_concurrent_start_turns_admit_only_one_provider(tmp_path):
    spawn = GatedSpawn(FakeProc(), FakeProc())
    adapter = _adapter(tmp_path, spawn=spawn)
    outcomes = []

    def attempt(turn_id):
        try:
            outcomes.append(adapter.start_turn(
                StartTurnRequest("p", run_id=turn_id, turn_id=turn_id)))
        except Exception as exc:  # owned race probe
            outcomes.append(exc)

    first = threading.Thread(target=attempt, args=("turn_a",))
    second = threading.Thread(target=attempt, args=("turn_b",))
    try:
        first.start()
        assert _wait_until(lambda: len(spawn.calls) == 1)
        second.start()
        assert _wait_until(
            lambda: len(spawn.calls) == 2
            or any(isinstance(item, RuntimeError) for item in outcomes))
        spawn.release.set()
        first.join(5)
        second.join(5)
        assert len(spawn.calls) == 1
        accepted = [item for item in outcomes if not isinstance(item, Exception)]
        refused = [item for item in outcomes if isinstance(item, RuntimeError)]
        assert len(accepted) == 1 and len(refused) == 1
        assert "already running" in str(refused[0])
        assert spawn.processes[0].terminate_called is False
        assert spawn.processes[0].kill_called is False
    finally:
        spawn.release.set()
        adapter.close()


def test_close_during_spawn_refuses_and_stops_the_returned_process(tmp_path):
    process = FakeProc()
    spawn = GatedSpawn(process)
    adapter = _adapter(tmp_path, spawn=spawn)
    outcome = []

    def attempt():
        try:
            outcome.append(adapter.start_turn(
                StartTurnRequest("p", run_id="run_a", turn_id="turn_a")))
        except Exception as exc:  # owned race probe
            outcome.append(exc)

    worker = threading.Thread(target=attempt)
    try:
        worker.start()
        assert _wait_until(lambda: len(spawn.calls) == 1)
        adapter.close()
        spawn.release.set()
        worker.join(5)
        assert len(outcome) == 1 and isinstance(outcome[0], RuntimeError)
    finally:
        spawn.release.set()
        worker.join(5)
    assert "closed during process startup" in str(outcome[0])
    assert process.terminate_called or process.kill_called
    assert adapter._active is None and adapter._process is None


def test_partial_worker_start_failure_preserves_error_and_stops_process(tmp_path, monkeypatch):
    process = FakeProc()
    adapter = _adapter(tmp_path, spawn=FakeSpawn(process))
    created = []
    real_thread = threading.Thread

    class FailingStart(real_thread):
        def start(self):
            if len(created) >= 1:
                raise RuntimeError("OWNED-THREAD-START-FAILURE")
            created.append(self)
            super().start()

    monkeypatch.setattr(adapter_module, "threading", SimpleNamespace(
        Thread=FailingStart, current_thread=threading.current_thread))
    try:
        with pytest.raises(RuntimeError, match="OWNED-THREAD-START-FAILURE"):
            adapter.start_turn(StartTurnRequest("p", run_id="run_a", turn_id="turn_a"))
        assert process.terminate_called or process.kill_called
        assert adapter._active is None and adapter._process is None
    finally:
        monkeypatch.undo()
        adapter.close()


def test_late_thread_started_cannot_rewrite_resume_identity(tmp_path):
    first, second = StreamedProc(), StreamedProc()
    adapter = _adapter(tmp_path, spawn=FakeSpawn(first, second))
    try:
        handle_a = adapter.start_turn(StartTurnRequest("a", run_id="run_a", turn_id="turn_a"))
        first.lines.push(_line({"type": "thread.started", "thread_id": "provider_a"}))
        assert _wait_until(lambda: adapter._provider_session_id == "provider_a")
        first.lines.push(_line({"type": "turn.completed", "usage": {}}))
        assert _wait_until(lambda: handle_a.done)
        handle_b = adapter.start_turn(StartTurnRequest("b", run_id="run_b", turn_id="turn_b"))
        second.lines.push(_line({"type": "thread.started", "thread_id": "provider_b"}))
        assert _wait_until(lambda: adapter._provider_session_id == "provider_b")
        first.lines.push(_line({"type": "thread.started", "thread_id": "provider_stale"}))
        time.sleep(0.3)
        assert adapter._provider_session_id == "provider_b"
        assert adapter._session.provider_session_id == "provider_b"
        assert not handle_b.done
    finally:
        adapter.close()


def test_header_after_turn_started_is_refused(tmp_path):
    first = StreamedProc()
    adapter = _adapter(tmp_path, spawn=FakeSpawn(first))
    try:
        handle = adapter.start_turn(StartTurnRequest("a", run_id="run_a", turn_id="turn_a"))
        first.lines.push(_line({"type": "turn.started"}))
        assert _wait_until(lambda: handle.state == "RUNNING")
        first.lines.push(_line({"type": "thread.started", "thread_id": "provider_late"}))
        time.sleep(0.25)
        assert adapter._provider_session_id == ""
    finally:
        adapter.close()


def test_stale_reader_cannot_mutate_usage_or_start_artifacts(tmp_path):
    first, second = StreamedProc(), StreamedProc()
    adapter = _adapter(tmp_path, spawn=FakeSpawn(first, second))
    try:
        handle_a = adapter.start_turn(StartTurnRequest("a", run_id="run_a", turn_id="turn_a"))
        first.lines.push(_line({"type": "thread.started", "thread_id": "provider_a"}))
        first.lines.push(_line({"type": "turn.completed", "usage": {"input_tokens": 7}}))
        assert _wait_until(lambda: handle_a.done)
        _drain(adapter)
        adapter.start_turn(StartTurnRequest("b", run_id="run_b", turn_id="turn_b"))
        second.lines.push(_line({"type": "thread.started", "thread_id": "provider_b"}))
        assert _wait_until(lambda: adapter._provider_session_id == "provider_b")
        _drain(adapter)
        before = dict(adapter._usage)
        first.lines.push(_line({"type": "item.completed", "item": {
            "id": "item_x", "type": "command_execution", "command": "cat big",
            "aggregated_output": "x" * 40000, "exit_code": 0, "status": "completed"}}))
        first.lines.push(_line({"type": "turn.completed", "usage": {"input_tokens": 999}}))
        time.sleep(0.4)
        events = _drain(adapter)
        assert adapter._usage.get("input_tokens") == before.get("input_tokens")
        assert not any(event.kind == "artifact.created" for event in events)
        assert not any(event.run_id == "run_a" for event in events)
    finally:
        adapter.close()


def test_stale_message_cannot_enter_the_active_turn(tmp_path):
    first, second = StreamedProc(), StreamedProc()
    adapter = _adapter(tmp_path, spawn=FakeSpawn(first, second))
    try:
        handle_a = adapter.start_turn(StartTurnRequest("a", run_id="run_a", turn_id="turn_a"))
        first.lines.push(_line({"type": "thread.started", "thread_id": "provider_a"}))
        first.lines.push(_line({"type": "turn.completed", "usage": {}}))
        assert _wait_until(lambda: handle_a.done)
        adapter.start_turn(StartTurnRequest("b", run_id="run_b", turn_id="turn_b"))
        second.lines.push(_line({"type": "thread.started", "thread_id": "provider_b"}))
        assert _wait_until(lambda: adapter._provider_session_id == "provider_b")
        _drain(adapter)
        first.lines.push(_line({"type": "item.completed", "item": {
            "id": "stale_item", "type": "agent_message", "text": "STALE BODY"}}))
        time.sleep(0.3)
        events = _drain(adapter)
        assert not any(event.run_id == "run_a" for event in events)
    finally:
        adapter.close()


def test_interrupt_request_scope_must_match_the_active_turn(tmp_path):
    process = BlockingProc()
    adapter = _adapter(tmp_path, spawn=FakeSpawn(process))
    try:
        handle = adapter.start_turn(StartTurnRequest("wait", run_id="run_a", turn_id="turn_a"))
        with pytest.raises(ValueError, match="active turn scope"):
            adapter.interrupt_turn({"turn_id": handle.turn_id, "session_id": "other"})
        with pytest.raises(ValueError, match="non-empty string"):
            adapter.interrupt_turn({"turn_id": handle.turn_id, "run_id": ""})
        assert adapter._interrupt_requested is False
        assert process.kill_called is False and process.terminate_called is False
        adapter.interrupt_turn(InterruptRequest(handle.turn_id))
        assert handle.wait(1) and process.kill_called
    finally:
        adapter.close()


def test_interrupt_refuses_when_target_changes_before_admission(tmp_path, monkeypatch):
    process = BlockingProc()
    adapter = _adapter(tmp_path, spawn=FakeSpawn(process))
    try:
        handle = adapter.start_turn(StartTurnRequest("wait", run_id="run_a", turn_id="turn_a"))
        original = adapter._start_provider_turn
        calls = []

        def replace(current):
            calls.append(current)
            if len(calls) == 1:
                adapter._active, adapter._process = None, None
            return original(current)

        monkeypatch.setattr(adapter, "_start_provider_turn", replace)
        with pytest.raises(RuntimeError, match="interrupt target changed before admission"):
            adapter.interrupt_turn(InterruptRequest(handle.turn_id))
        assert adapter._interrupt_requested is False
        assert process.kill_called is False and process.terminate_called is False
    finally:
        monkeypatch.undo()
        adapter.close()


def test_interrupt_does_not_stop_or_flag_a_replacement_turn(tmp_path, monkeypatch):
    first, second = BlockingProc(), BlockingProc()
    adapter = _adapter(tmp_path, spawn=FakeSpawn(first, second))
    try:
        handle_a = adapter.start_turn(StartTurnRequest("a", run_id="run_a", turn_id="turn_a"))
        original = adapter._ensure_session_event
        swapped = []

        def swap(current):
            if not swapped:
                swapped.append(True)
                current.state = "SUCCEEDED"
                current._done.set()
                adapter.start_turn(StartTurnRequest("b", run_id="run_b", turn_id="turn_b"))
            return original(current)

        monkeypatch.setattr(adapter, "_ensure_session_event", swap)
        with pytest.raises(RuntimeError, match="interrupt target changed before admission"):
            adapter.interrupt_turn(InterruptRequest(handle_a.turn_id))
        assert adapter._interrupt_requested is False
        assert second.kill_called is False and second.terminate_called is False
        assert adapter._active is not None and adapter._active.turn_id == "turn_b"
    finally:
        monkeypatch.undo()
        adapter.close()


def test_confirmed_terminal_wins_a_late_interrupt(tmp_path):
    process = FakeProc([
        _line({"type": "thread.started", "thread_id": "provider_a"}),
        _line({"type": "turn.completed", "usage": {}})])
    adapter = _adapter(tmp_path, spawn=FakeSpawn(process))
    try:
        handle = adapter.start_turn(StartTurnRequest("a", run_id="run_a", turn_id="turn_a"))
        assert handle.wait(2)
        assert process.terminate_called is False and process.kill_called is False
        with pytest.raises(RuntimeError, match="cannot interrupt a terminal turn"):
            adapter.interrupt_turn(InterruptRequest(handle.turn_id))
    finally:
        adapter.close()


def test_closed_adapter_refuses_start_before_spawn(tmp_path):
    spawn = FakeSpawn(FakeProc())
    adapter = _adapter(tmp_path, spawn=spawn)
    adapter.close()
    with pytest.raises(RuntimeError, match="closed"):
        adapter.start_turn(StartTurnRequest("p", run_id="run_a", turn_id="turn_a"))
    assert spawn.calls == []
