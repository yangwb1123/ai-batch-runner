"""Real local pipe backpressure and process-tree cleanup, without providers."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from pbatch.tui_codex_support import _stop_process
from pbatch.tui_lifecycle import RuntimeOperation
from pbatch.tui_pi_rpc_support import DefaultPiRpcTransport
from pbatch import tui_appserver_support
from pbatch.tui_adapter_appserver import CodexAppServerAdapter
from pbatch.tui_runtime import TurnHandle


def _transport(kind, tmp_path, monkeypatch):
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    if kind == "app-server":
        monkeypatch.setattr(tui_appserver_support, "app_server_argv", lambda _sock: argv)
        return tui_appserver_support.DefaultTransport(cwd=str(tmp_path))
    def spawn(_argv, cwd):
        return subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
    return DefaultPiRpcTransport(str(tmp_path), spawn=spawn)


@pytest.mark.parametrize("kind", ["pi-rpc", "app-server"])
def test_deadline_closes_transport_while_large_stdin_write_is_blocked(
        tmp_path, monkeypatch, kind):
    transport = _transport(kind, tmp_path, monkeypatch)
    errors = []
    def send():
        try:
            transport.send_line("x" * (512 * 1024) + "\n")
        except (OSError, ValueError) as exc:
            errors.append(exc)
    writer = threading.Thread(target=send, daemon=True)
    writer.start()
    try:
        time.sleep(0.05)
        assert writer.is_alive() and transport._process.poll() is None
        operation = RuntimeOperation(transport, 0.05, "blocked write")
        assert operation._closed.wait(2), "close waited on the blocked writer's lock"
        operation.finish()
        writer.join(1)
        assert transport._process.poll() is not None
        assert not writer.is_alive() and errors
        assert "timed out" in operation.reason
    finally:
        _stop_process(transport._process)
        writer.join(1)
        transport.close()


def test_pending_approval_denial_cannot_block_watchdog_teardown(tmp_path, monkeypatch):
    transport = _transport("app-server", tmp_path, monkeypatch)
    adapter = CodexAppServerAdapter(str(tmp_path), transport=transport)
    adapter._initialized = True
    adapter._session.provider_session_id = "thread"
    handle = TurnHandle(adapter.session.session_id, "run", "turn", "agent_main", "now")
    adapter._active = handle
    entry = adapter._new_approval_entry(
        1, "item/commandExecution/requestApproval", "command_execution", {})
    adapter._approvals[(int, 1)] = entry
    def send():
        try:
            adapter._send({"method": "test", "params": {"text": "x" * (512 * 1024)}})
        except OSError:
            pass
    writer = threading.Thread(target=send, daemon=True)
    writer.start()
    try:
        time.sleep(0.05)
        assert writer.is_alive()
        operation = RuntimeOperation(adapter, 0.05, "blocked approval")
        operation.bind(handle)
        assert operation._closed.wait(2), "approval decline blocked transport close"
        writer.join(1)
        assert not writer.is_alive()
        assert transport._process.poll() is not None
        assert not adapter.pending_approvals
    finally:
        _stop_process(transport._process)
        adapter.close()
        writer.join(1)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process groups")
@pytest.mark.parametrize("leader_already_exited", [False, True])
def test_stop_reaps_sigterm_resistant_descendant_after_leader_exit(
        tmp_path, leader_already_exited):
    ready = tmp_path / "ready"
    child_path = tmp_path / "child.pid"
    code = ("import os,signal,time,pathlib\n"
            "child=os.fork()\n"
            "if child == 0:\n"
            " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            f" pathlib.Path({str(ready)!r}).touch()\n"
            " time.sleep(30)\n"
            "else:\n"
            f" pathlib.Path({str(child_path)!r}).write_text(str(child))\n"
            " time.sleep(30)\n")
    process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        if leader_already_exited:
            process.terminate()
            process.wait(timeout=1)
        _stop_process(process)
        assert process.poll() is not None
        child = int(child_path.read_text())
        deadline = time.monotonic() + 2
        stat = Path(f"/proc/{child}/stat")
        while stat.exists() and time.monotonic() < deadline:
            if stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.01)
        if stat.exists():
            assert stat.read_text().split()[2] == "Z"
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)
