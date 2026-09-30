"""Real subprocess resource boundaries for CLI and validation long-running use."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from pbatch import process_exec, runner


def _python(code):
    return [sys.executable, "-c", code]


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -float("inf"),
                                      10 ** 1000, True, "1", -1])
@pytest.mark.parametrize("api", ["argv", "validation"])
def test_invalid_deadline_is_rejected_before_spawn(tmp_path, monkeypatch, timeout, api):
    def forbidden_spawn(*_args, **_kwargs):
        pytest.fail("invalid deadline started a subprocess")

    monkeypatch.setattr(process_exec.subprocess, "Popen", forbidden_spawn)
    command, invoke = ("echo ok", runner.run_validation) if api == "validation" else (
        _python("print('ok')"), runner.run_argv)
    with pytest.raises(ValueError, match="finite and non-negative"):
        invoke(command, str(tmp_path), timeout=timeout)


def test_pre_cancelled_validator_does_not_spawn(tmp_path, monkeypatch):
    def forbidden_spawn(*_args, **_kwargs):
        pytest.fail("already cancelled validator started a subprocess")

    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(process_exec.subprocess, "Popen", forbidden_spawn)
    result = runner.run_validation("echo ignored", str(tmp_path), cancel_event=cancelled)
    assert not result.ok and not result.timed_out
    assert "cancelled" in result.stderr


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_overflow_without_newline_stops_before_deadline(tmp_path, stream):
    code = (f"import sys,time;sys.{stream}.write('x'*2048);"
            f"sys.{stream}.flush();time.sleep(30)")
    start = time.monotonic()
    result = runner.run_argv(_python(code), str(tmp_path), timeout=3, cap=1024)
    assert time.monotonic() - start < 1.5
    assert not result.ok and not result.timed_out
    assert "process output exceeds 1024 bytes" in result.stderr
    assert len(result.stdout.encode()) <= 1024


def test_invalid_utf8_is_reported_and_split_unicode_survives(tmp_path):
    code = ("import os,time;os.write(1,b'\\xff');"
            "os.write(1,b'\\xe4');time.sleep(.06);os.write(1,b'\\xb8\\xad\\r');"
            "time.sleep(.06);os.write(1,b'\\n')")
    result = runner.run_argv(_python(code), str(tmp_path), timeout=3)
    assert result.ok and result.stdout == "\ufffd中\n"


def _is_live(pid):
    # exists() then read_text() is a TOCTOU: the process can exit between the
    # two, and a zombie child is exactly what this helper is called to notice.
    # Fall through to the signal probe when the read loses the race.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_stopped(pid):
    deadline = time.monotonic() + .5
    while _is_live(pid) and time.monotonic() < deadline:
        time.sleep(.01)
    return not _is_live(pid)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="uses Linux process state")
@pytest.mark.parametrize("keep_pipes", [False, True])
def test_exited_leader_does_not_leave_group_child(tmp_path, keep_pipes):
    child_pid = tmp_path / "child.pid"
    code = ("import os,time\n"
            "pid=os.fork()\n"
            "if pid == 0:\n"
            + (" pass\n" if keep_pipes else " os.close(1);os.close(2)\n")
            + f" open({str(child_pid)!r},'w').write(str(os.getpid()))\n"
            " time.sleep(30)\n"
            "else:\n"
            f" while not os.path.exists({str(child_pid)!r}): time.sleep(.01)\n"
            f" while not os.path.getsize({str(child_pid)!r}): time.sleep(.01)\n"
            " print('leader finished')\n")
    pid = None
    try:
        result = runner.run_argv(_python(code), str(tmp_path), timeout=.4)
        pid = int(child_pid.read_text())
        assert result.timed_out is keep_pipes
        assert result.ok is not keep_pipes
        assert _wait_stopped(pid)
    finally:
        if pid is not None and _is_live(pid):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("finish", ["timeout", "cancel", "overflow", "success"])
def test_bounded_process_reaps_child_and_closes_both_pipes(tmp_path, monkeypatch, finish):
    processes = []
    real_spawn = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(process_exec.subprocess, "Popen", capture)
    cancel = threading.Event()
    timer = threading.Timer(.1, cancel.set)
    code = "print('done')" if finish == "success" else "import time;time.sleep(30)"
    if finish == "overflow":
        code = "import sys,time;sys.stdout.write('x'*2048);sys.stdout.flush();time.sleep(30)"
    if finish == "cancel":
        timer.start()
    try:
        result = process_exec.run_bounded(
            _python(code), str(tmp_path), .3, 1024, False, "process", cancel)
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join(1)
    assert result.ok is (finish == "success")
    assert result.timed_out is (finish == "timeout")
    assert len(processes) == 1 and processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


@pytest.mark.parametrize("cancelled", [False, True])
def test_closed_output_pipes_do_not_disable_deadline_or_cancel(tmp_path, cancelled):
    cancel = threading.Event()
    timer = threading.Timer(.1, cancel.set)
    if cancelled:
        timer.start()
    started = time.monotonic()
    try:
        result = process_exec.run_bounded(
            _python("import os,time;os.close(1);os.close(2);time.sleep(30)"),
            str(tmp_path), .3, 1024, False, "process", cancel)
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join(1)
    assert time.monotonic() - started < 1.5
    assert not result.ok and result.timed_out is not cancelled


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_live_agent_overflow_stops_immediately_and_rejects(tmp_path, monkeypatch, stream):
    processes = []
    code = (f"import sys,time;sys.{stream}.write('x'*2048);"
            f"sys.{stream}.flush();time.sleep(30)")

    def spawn(_cmd, workdir, _env):
        process = subprocess.Popen(_python(code), cwd=workdir, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        processes.append(process)
        return process

    monkeypatch.setattr(runner, "_spawn_agent", spawn)
    monkeypatch.setattr(runner.config, "OUTPUT_MAX_BYTES", 1024)
    monkeypatch.setattr(runner.config, "MEMORY_MODE", "off")
    monkeypatch.setattr(runner.config, "STREAM_OUTPUT", "none")
    started = time.monotonic()
    result = runner.run_task(runner.Task(prompt="bounded", cwd=str(tmp_path), timeout=3))
    assert time.monotonic() - started < 1.5
    assert not result.success and "output exceeds" in result.reason
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_missing_killpg_uses_direct_child_fallback(monkeypatch):
    calls = []

    class Child:
        def kill(self):
            calls.append("kill")

        def wait(self, timeout):
            calls.append(("wait", timeout))

    monkeypatch.delattr(process_exec.os, "killpg", raising=False)
    runner.kill_proc_group(Child())
    assert calls == ["kill", ("wait", 1)]


def test_corrupt_agent_output_is_rejected_before_artifact(tmp_path, monkeypatch):
    def spawn(_cmd, workdir, _env):
        return subprocess.Popen(_python("import os;os.write(1,b'\\xff')"),
                                cwd=workdir, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)

    monkeypatch.setattr(runner, "_spawn_agent", spawn)
    monkeypatch.setattr(runner.config, "MEMORY_MODE", "off")
    task = runner.Task(prompt="corrupt", cwd=str(tmp_path),
                       output=str(tmp_path / "artifact.md"), timeout=3)
    result = runner.run_task(task)
    assert not result.success and "output read failed" in result.reason
    assert "decode" in result.stderr
    runner.save_result(task, result)
    assert not Path(task.output).exists()


def test_nonfinite_agent_deadline_does_not_start_provider(tmp_path, monkeypatch):
    def forbidden_spawn(*_args, **_kwargs):
        pytest.fail("invalid agent timeout started a provider")

    monkeypatch.setattr(runner, "_spawn_agent", forbidden_spawn)
    result = runner.run_task(runner.Task(prompt="invalid", cwd=str(tmp_path), timeout=float("nan")))
    assert not result.success and "finite and non-negative" in result.reason


@pytest.mark.parametrize("embedded", [None, ["binary path", "--_bundled-script",
                                             "scripts/check.py", "file path"]])
@pytest.mark.parametrize("cancelled", [False, True])
def test_validator_uses_argv_only_for_embedded_literal_command(
        tmp_path, monkeypatch, embedded, cancelled):
    calls = []
    event = threading.Event() if cancelled else None
    command = "original shell command"

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return process_exec.ValidationResult(True, 0, "", "", False)

    monkeypatch.setattr(runner, "embedded_checker_argv", lambda _command: embedded)
    monkeypatch.setattr(runner, "run_bounded", fake_run)
    assert runner.run_validation(command, str(tmp_path), cancel_event=event).ok
    args, kwargs = calls[0]
    assert args[0] == (command if embedded is None else embedded)
    assert args[4] is (embedded is None)
    assert kwargs == ({} if event is None else {"cancel_event": event})


@pytest.mark.parametrize("chunks", [
    ['{"type":"thread.started","thread_id":"01a0272d-66ab-74f1-8891-1013ac2cb87a"}\n'
     '{"type":"turn.completed"}\n'],
    ['{"type":"thread.star', 'ted","thread_id":"01a0272d-66ab-74f1-8891-1013ac2cb87a"}\n',
     '{"type":"turn.completed"}\n'],
])
def test_native_thread_capture_handles_arbitrary_pipe_chunks(tmp_path, monkeypatch, chunks):
    from pbatch import runner_session, session

    monkeypatch.setattr(runner_session.config, "agent_capture_thread_id", lambda _agent: True)
    assert runner_session._capture_codex_thread(
        chunks, "work", "logical-session", str(tmp_path), "custom-codex")
    thread, found = session.resolve_provider_session(
        str(tmp_path), "work", "logical-session", "continue", "custom-codex")
    assert found and thread == "01a0272d-66ab-74f1-8891-1013ac2cb87a"
