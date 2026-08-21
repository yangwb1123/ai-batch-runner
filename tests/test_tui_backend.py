"""Focused regression tests for the TUI conversation backend."""

from __future__ import annotations

import os
import json
import threading
from pathlib import Path

from pbatch import config
from pbatch.lock import acquire_lock, release_lock
from pbatch.tui_backend import TUIBackend


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
