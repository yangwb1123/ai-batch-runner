"""Focused regression tests for the TUI conversation backend."""

from __future__ import annotations

import os
import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

from pbatch import config, runner
from pbatch.lock import acquire_lock, release_lock
from pbatch.tui_backend import TUIBackend


def _runtime_event(kind, **payload):
    return SimpleNamespace(kind=kind, payload=payload)


class FakeProc:
    def __init__(self, events=()):
        self.events = queue.Queue()
        for event in events:
            self.events.put(event)


class FakeStructuredAdapter:
    def __init__(self, proc, provider_session_id=""):
        self.proc = proc
        self.started = threading.Event()
        self.interrupt_calls = []
        self.closed = False
        self.resume_requests = []
        self.session_calls = 0
        self._provider_session_id = provider_session_id

    def initialize(self, request):
        self.initialize_request = request

    def start_session(self, request):
        self.session_request = request
        self.session_calls += 1
        return SimpleNamespace(session_id=request["session_id"])

    def resume_session(self, request):
        self.resume_requests.append(request)
        self._provider_session_id = request["provider_session_id"]
        return SimpleNamespace(session_id="resumed")

    def session(self):
        return SimpleNamespace(provider_session_id=self._provider_session_id)

    def start_turn(self, request):
        self.turn_request = request
        self.handle = SimpleNamespace(turn_id="fake-turn")
        self.started.set()
        return self.handle

    def events(self):
        while True:
            event = self.proc.events.get(timeout=3)
            if event is None:
                return
            yield event
            if event.kind in {"turn.completed", "turn.failed", "turn.interrupted"}:
                return

    def interrupt_turn(self, handle):
        self.interrupt_calls.append(handle)
        self.proc.events.put(_runtime_event("turn.interrupted"))

    def close(self):
        self.closed = True
        self.proc.events.put(None)


def _agent(tmp_path: Path, body: str, name: str = "agent.sh") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_turns_reuse_session_stream_and_restore_config(tmp_path, capsys):
    args_log = tmp_path / "args.log"
    agent = _agent(
        tmp_path,
        f"printf '%s\\n' \"$@\" >> '{args_log}'\n"
        "printf 'answer\\n'\nprintf 'diagnostic\\n' >&2\n")
    events = []
    original = (config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT)
    original_logging = (list(config.log.handlers), config.log.propagate,
                        config.log.level)
    backend = TUIBackend(
        lambda kind, text: events.append((kind, text)), cwd=str(tmp_path),
        agent=str(agent), model="model-a", provider="provider-a",
        memory_mode="off", timeout=10, session_id="session-fixed",
        session_name="chat-fixed")

    first = backend.run_turn("first prompt")
    second = backend.run_turn("second prompt")

    assert first.success and second.success
    assert first.session_id == second.session_id == "session-fixed"
    assert first.session_name == second.session_name == "chat-fixed"
    assert (config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT) == original
    assert (list(config.log.handlers), config.log.propagate,
            config.log.level) == original_logging
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert any(kind == "stdout" and "answer" in text for kind, text in events)
    assert any(kind == "stderr" and "diagnostic" in text for kind, text in events)
    assert any(kind == "log" and "OK  done" in text for kind, text in events)
    argv = args_log.read_text(encoding="utf-8").splitlines()
    assert argv == [
        "-p", "first prompt", "--model", "model-a", "--provider", "provider-a",
        "--session-id", "session-fixed", "--name", "chat-fixed",
        "-p", "second prompt", "--model", "model-a", "--provider", "provider-a",
        "--session-id", "session-fixed",
    ]
    state = backend.snapshot()
    assert state["status"] == "succeeded"
    assert state["turn"] == 2 and not state["running"]
    assert state["last_result"]["success"] is True


def test_new_session_resets_turn_and_name(tmp_path):
    agent = _agent(tmp_path, "echo ok\n")
    backend = TUIBackend(cwd=str(tmp_path), agent=str(agent), memory_mode="off",
                         session_id="old", session_name="old-name")
    assert backend.run_turn("hello").success

    state = backend.new_session(name="fresh", session_id="new-fixed")

    assert state["status"] == "idle"
    assert state["turn"] == 0
    assert state["session_id"] == "new-fixed"
    assert state["session_name"] == "fresh"
    assert state["last_result"] is None


def test_emit_callback_can_be_attached_for_embedded_ui(tmp_path):
    agent = _agent(tmp_path, "echo attached\n")
    events = []
    backend = TUIBackend(cwd=str(tmp_path), agent=str(agent),
                         memory_mode="off", session_id="embedded-session")

    backend.set_emit(lambda kind, text: events.append((kind, text)))
    result = backend.run_turn("hello")

    assert result.success
    assert any(kind == "stdout" and "attached" in text
               for kind, text in events)


def test_snapshot_reports_missing_agent_without_blocking_startup(tmp_path):
    backend = TUIBackend(cwd=str(tmp_path), agent="definitely-missing-agent",
                         memory_mode="off", session_id="missing-session")

    state = backend.snapshot()

    assert state["agent_ready"] is False
    assert "not found" in state["agent_error"]
    assert state["status"] == "idle"


def test_turn_respects_workspace_lock(tmp_path):
    agent = _agent(tmp_path, "echo should-not-run\n")
    held = acquire_lock(str(tmp_path))
    try:
        backend = TUIBackend(cwd=str(tmp_path), agent=str(agent),
                             memory_mode="off", session_id="locked-session")
        result = backend.run_turn("do not start")
        assert not result.success and result.returncode == 5
        assert result.reason == "workspace is locked (exit 5)"
        assert (tmp_path / ".pi-batch.lock").exists()
    finally:
        release_lock(held)


def test_successful_turn_enters_scoped_memory_index(tmp_path):
    agent = _agent(tmp_path, "echo remembered\n")
    backend = TUIBackend(cwd=str(tmp_path), agent=str(agent),
                         memory_mode="on", session_id="memory-session")

    result = backend.run_turn("remember this turn")

    assert result.success
    index = tmp_path / ".pi-batch" / "memory" / "sessions.index.jsonl"
    event = json.loads(index.read_text(encoding="utf-8").splitlines()[-1])
    assert event["type"] == "task" and event["status"] == "PASSED"
    assert event["session_id"] == "memory-session"
    assert event["prompt_excerpt"] == "remember this turn"
    assert not (tmp_path / ".pi-batch.lock").exists()


def test_failed_turn_is_reported_and_config_is_restored(tmp_path, capsys):
    agent = _agent(tmp_path, "echo rejected\necho details >&2\nexit 7\n")
    original = (config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT)
    events = []
    backend = TUIBackend(
        lambda kind, text: events.append((kind, text)), cwd=str(tmp_path),
        agent=str(agent), memory_mode="off", session_id="failed-session")

    result = backend.run_turn("fail")

    assert not result.success and result.returncode == 7
    assert "agent exited 7" in result.reason
    assert backend.snapshot()["status"] == "failed"
    assert any(kind == "stderr" and "details" in text for kind, text in events)
    assert any(kind == "log" and "FAIL" in text for kind, text in events)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert (config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT) == original


def test_cancel_stops_active_turn(tmp_path):
    pid_file = tmp_path / "pid"
    agent = _agent(
        tmp_path,
        f"echo $$ > '{pid_file}'\necho READY\nsleep 30\necho LATE\n",
        "slow-agent.sh")
    ready = threading.Event()
    events = []

    def emit(kind: str, text: str) -> None:
        events.append((kind, text))
        if kind == "stdout" and "READY" in text:
            ready.set()

    backend = TUIBackend(emit, cwd=str(tmp_path), agent=str(agent),
                         memory_mode="off", timeout=60,
                         session_id="cancel-session")
    results = []
    worker = threading.Thread(target=lambda: results.append(
        backend.run_turn("wait")))
    worker.start()
    assert ready.wait(5), events

    assert backend.cancel() is True
    worker.join(5)

    assert not worker.is_alive()
    assert results and not results[0].success
    assert results[0].reason == "cancelled"
    state = backend.snapshot()
    assert state["status"] == "cancelled" and not state["running"]
    assert any(item == ("status", "cancelling") for item in events)
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"cancelled agent still alive: {child_pid}")


def test_cancel_kills_only_processes_added_during_this_turn(tmp_path, monkeypatch):
    existing = object()
    owned = object()
    invoked = threading.Event()
    released = threading.Event()
    killed = []

    def snapshot():
        return (existing, owned) if invoked.is_set() else (existing,)

    def kill_one(proc):
        killed.append(proc)
        released.set()

    def reject_global_cancel():
        raise AssertionError("TUI cancel must not use the global process cleanup")

    monkeypatch.setattr(runner, "proc_snapshot", snapshot)
    monkeypatch.setattr(runner, "kill_proc_group", kill_one)
    monkeypatch.setattr(runner, "kill_active_procs", reject_global_cancel)
    backend = TUIBackend(cwd=str(tmp_path), agent="fake-agent",
                         memory_mode="off", session_id="scoped-cancel")

    def invoke(task, _turn, _flags_kind):
        invoked.set()
        released.wait(3)
        return backend._cancelled_result(task)

    monkeypatch.setattr(backend, "_invoke_runner", invoke)
    results = []
    worker = threading.Thread(target=lambda: results.append(
        backend.run_turn("wait")))
    worker.start()
    assert invoked.wait(1)

    assert backend.cancel() is True
    assert backend.cancel() is False
    worker.join(3)

    assert not worker.is_alive()
    assert killed == [owned]
    assert results and results[0].reason == "cancelled"
    assert backend._turn_procs == ()


def test_structured_turn_projects_typed_events_and_returns_success(tmp_path):
    events = []
    proc = FakeProc([
        _runtime_event("command.started", item_id="cmd-1", command="pytest"),
        _runtime_event("command.output", item_id="cmd-1",
                       aggregated_output="\\\x00" * 5000),
        _runtime_event("command.completed", item_id="cmd-1", command="pytest",
                       exit_code=0),
        _runtime_event("file.changed", changes=[{"path": "pbatch/tui.py"}]),
        _runtime_event("usage.updated", usage={"input_tokens": 10,
                                                "output_tokens": 4,
                                                "total_tokens": 14}),
        _runtime_event("message.delta", text="structured answer"),
        _runtime_event("turn.completed", elapsed_ms=15,
                       usage={"input_tokens": 10, "output_tokens": 4}),
    ])
    adapter = FakeStructuredAdapter(proc)
    backend = TUIBackend(
        lambda kind, text: events.append((kind, text)), cwd=str(tmp_path),
        agent="codex", memory_mode="off", session_id="structured-session")

    result = backend.run_turn_structured(
        "implement", adapter_factory=lambda: adapter)

    assert result.success and result.stdout == "structured answer"
    assert result.session_id == "structured-session"
    assert adapter.closed
    kinds = [kind for kind, _text in events]
    for kind in ("command", "command-output", "file", "usage", "stdout"):
        assert kind in kinds
    command = next(json.loads(text) for kind, text in events
                   if kind == "command" and '"phase":"completed"' in text)
    assert command["command"] == "pytest" and command["exit_code"] == 0
    outputs = [text for kind, text in events if kind == "command-output"]
    assert outputs and all(len(text.encode("utf-8")) <= 2 * 1024 for text in outputs)
    usages = [json.loads(text) for kind, text in events if kind == "usage"]
    assert usages[0]["total_tokens"] == 14
    assert backend.snapshot()["status"] == "succeeded"
    assert not (tmp_path / ".pi-batch.lock").exists()


def test_structured_turn_resumes_previous_provider_thread(tmp_path):
    """Multi-turn chats carry context: turn 1 captures the provider
    thread id on the backend; turn 2 resumes it (T5/T6 headless semantics
    wired into the TUI structured path)."""
    def turn_events():
        return [
            _runtime_event("message.delta", text="answer"),
            _runtime_event("turn.completed", elapsed_ms=5),
        ]

    adapters = []
    backend = TUIBackend(cwd=str(tmp_path), agent="codex", memory_mode="off",
                         session_id="multi-turn")

    def factory():
        adapter = FakeStructuredAdapter(FakeProc(turn_events()),
                                        provider_session_id="thread-1")
        adapters.append(adapter)
        return adapter

    first = backend.run_turn_structured("first turn", adapter_factory=factory)
    assert first.success
    assert backend.provider_thread_id == "thread-1"
    assert adapters[0].session_calls == 1 and not adapters[0].resume_requests

    second = backend.run_turn_structured("second turn", adapter_factory=factory)
    assert second.success
    assert adapters[1].resume_requests == [{
        "provider_session_id": "thread-1", "workspace_id": str(tmp_path),
    }]
    assert adapters[1].session_calls == 0


def test_structured_resume_failure_falls_back_to_fresh_session(tmp_path):
    backend = TUIBackend(cwd=str(tmp_path), agent="codex", memory_mode="off",
                         session_id="resume-fail")
    backend.provider_thread_id = "thread-stale"
    adapters = []

    class BrokenResumeAdapter(FakeStructuredAdapter):
        def resume_session(self, request):
            raise RuntimeError("thread gone")

    def factory():
        adapter = BrokenResumeAdapter(
            FakeProc([_runtime_event(
                "message.delta", text="fresh"),
                _runtime_event("turn.completed", elapsed_ms=3)]))
        adapters.append(adapter)
        return adapter

    result = backend.run_turn_structured("retry", adapter_factory=factory)
    assert result.success
    assert adapters[0].session_calls == 1  # fell back to a fresh session


def test_structured_cancel_interrupts_current_adapter_handle(tmp_path):
    proc = FakeProc([_runtime_event(
        "command.started", item_id="cmd-1", command="long command")])
    adapter = FakeStructuredAdapter(proc)
    backend = TUIBackend(cwd=str(tmp_path), agent="codex", memory_mode="off",
                         session_id="structured-cancel")
    results = []
    worker = threading.Thread(target=lambda: results.append(
        backend.run_turn_structured(
            "wait", adapter_factory=lambda _cwd: adapter)))
    worker.start()
    assert adapter.started.wait(2)

    assert backend.cancel() is True
    worker.join(3)

    assert not worker.is_alive()
    assert adapter.interrupt_calls == [adapter.handle]
    assert results and not results[0].success
    assert results[0].reason == "cancelled"
    assert backend.snapshot()["status"] == "cancelled"
