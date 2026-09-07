"""Bounded subprocess execution shared by validators and control-plane tools.

The agent runner keeps its live-stream path separate, while validators and
argv utilities use this small deterministic path. Both paths share the same
hard deadline, process-group kill and bounded diagnostic contract.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import NamedTuple


class ValidationResult(NamedTuple):
    """Bounded command outcome; timeout/crash uses exit code ``-1``."""

    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class _CancelledDrain(RuntimeError):
    """A scoped cancellation killed the bounded child process."""

    def __init__(self, stdout_lines, stderr_lines, threads):
        self.stdout_lines = stdout_lines
        self.stderr_lines = stderr_lines
        self.threads = threads
        super().__init__("process cancelled")


class _DrainTimeout(subprocess.TimeoutExpired):
    """Timeout carrying collectors so partial output survives process kill."""

    def __init__(self, args, timeout, stdout_lines, stderr_lines,
                 overflow, threads):
        super().__init__(args, timeout)
        self.stdout_lines = stdout_lines
        self.stderr_lines = stderr_lines
        self.overflow = overflow
        self.threads = threads


def run_bounded(cmd, cwd: str, timeout: float, output_cap: int,
                shell: bool, label: str, cancel_event=None) -> ValidationResult:
    """Spawn one command, drain both pipes and fail closed on every boundary."""
    try:
        proc = subprocess.Popen(
            cmd, shell=shell, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
    except Exception as exc:
        return ValidationResult(False, -1, "", str(exc), False)
    start = time.monotonic()
    try:
        output = _drain_process(proc, start, timeout, output_cap, cancel_event)
    except _CancelledDrain as exc:
        _kill_group(proc)
        _join_drainers(exc.threads)
        return ValidationResult(False, -1, *_partial_output(exc), False)
    except _DrainTimeout as exc:
        _kill_group(proc)
        _join_drainers(exc.threads)
        stdout, stderr = _partial_output(exc)
        if any(exc.overflow):
            stderr += f"{label} output exceeds {output_cap} bytes"
        return ValidationResult(False, -1, stdout, stderr, True)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return ValidationResult(False, -1, "", "", True)
    stdout, stderr, overflow = output
    if any(overflow):
        stderr += f"{label} output exceeds {output_cap} bytes"
        return ValidationResult(False, -1, stdout, stderr, False)
    return ValidationResult(proc.returncode == 0, proc.returncode,
                            stdout, stderr, False)


def _partial_output(exc) -> tuple[str, str]:
    """Join the bounded chunks collected before/after the kill."""
    return "".join(exc.stdout_lines), "".join(exc.stderr_lines)


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the command group and reap its direct child."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def _read_stream(stream, collector: list, cap: int, overflow: list) -> None:
    """Drain a text pipe in fixed chunks while retaining only bounded data."""
    total = 0
    try:
        for line in iter(lambda: stream.readline(8192), ""):
            total += len(line.encode("utf-8", errors="replace"))
            if total <= cap:
                collector.append(line)
            else:
                overflow[0] = True
    except ValueError:
        pass
    finally:
        stream.close()


def _drain_process(proc: subprocess.Popen, start: float, timeout: float,
                   cap: int, cancel_event=None) -> tuple[str, str, tuple[bool, bool]]:
    """Drain both pipes against one absolute deadline."""
    stdout, stderr = [], []
    overflow = [[False], [False]]
    threads = (
        threading.Thread(target=_read_stream,
                         args=(proc.stdout, stdout, cap, overflow[0]),
                         daemon=True),
        threading.Thread(target=_read_stream,
                         args=(proc.stderr, stderr, cap, overflow[1]),
                         daemon=True),
    )
    for thread in threads:
        thread.start()
    deadline = start + timeout
    for thread in threads:
        while thread.is_alive():
            if _cancel_requested(cancel_event):
                _kill_group(proc)
                _join_drainers(threads)
                raise _CancelledDrain(stdout, stderr, threads)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _DrainTimeout(proc.args, timeout, stdout, stderr,
                                    (overflow[0][0], overflow[1][0]), threads)
            thread.join(timeout=min(0.05, remaining))
    while proc.poll() is None:
        if _cancel_requested(cancel_event):
            _kill_group(proc)
            _join_drainers(threads)
            raise _CancelledDrain(stdout, stderr, threads)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _DrainTimeout(proc.args, timeout, stdout, stderr,
                                (overflow[0][0], overflow[1][0]), threads)
        time.sleep(min(0.05, remaining))
    return "".join(stdout), "".join(stderr), (
        overflow[0][0], overflow[1][0])


def _cancel_requested(cancel_event) -> bool:
    try:
        return bool(cancel_event is not None and cancel_event.is_set())
    except (AttributeError, TypeError):
        return False


def _join_drainers(threads: tuple, maximum: float = 1.0) -> None:
    """Bound the post-kill wait while allowing partial pipe data to arrive."""
    deadline = time.monotonic() + max(0.0, maximum)
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
