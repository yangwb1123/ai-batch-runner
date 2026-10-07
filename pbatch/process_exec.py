"""Bounded subprocess execution shared by validators and control-plane tools.

The agent runner keeps its live-stream path separate, while validators and
argv utilities use this small deterministic path. Both paths share the same
hard deadline, process-group kill and bounded diagnostic contract.
"""

from __future__ import annotations

import codecs
import io
import math
import os
import signal
import subprocess
import threading
import time
from typing import NamedTuple

from .process_shutdown import (
    forget_process, interrupt_after_spawn, shutdown_requested, spawn_registered,
)


class ValidationResult(NamedTuple):
    """Bounded command outcome; timeout/crash uses exit code ``-1``."""

    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class StreamStatus(list):
    """Preserve the legacy overflow flag while reporting reader failures."""

    def __init__(self):
        super().__init__([False])
        self.error = ""


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
                shell: bool, label: str, cancel_event=None, *, env=None) -> ValidationResult:
    """Spawn one command, drain both pipes and fail closed on every boundary."""
    validate_timeout(timeout, label)
    if isinstance(output_cap, bool) or not isinstance(output_cap, int) or output_cap < 1:
        raise ValueError(f"{label} output cap must be a positive integer")
    if _cancel_requested(cancel_event):
        return ValidationResult(False, -1, "", f"{label} cancelled", False)
    try:
        proc = spawn_registered(lambda: subprocess.Popen(
            cmd, shell=shell, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            start_new_session=True, **({"env": env} if env is not None else {})), _kill_group)
    except Exception as exc:
        interrupt_after_spawn()
        return ValidationResult(False, -1, "", str(exc), False)
    start = time.monotonic()
    try:
        interrupt_after_spawn()
        output = _drain_process(proc, start, timeout, output_cap, cancel_event)
    except _CancelledDrain as exc:
        stdout, stderr = _partial_output(exc)
        return ValidationResult(False, -1, stdout, stderr + f"{label} cancelled", False)
    except _DrainTimeout as exc:
        stdout, stderr = _partial_output(exc)
        if any(exc.overflow):
            stderr += f"{label} output exceeds {output_cap} bytes"
        return ValidationResult(False, -1, stdout, stderr, True)
    except subprocess.TimeoutExpired:
        return ValidationResult(False, -1, "", "", True)
    except Exception as exc:
        return ValidationResult(False, -1, "", str(exc), False)
    finally:
        _kill_group(proc)
        forget_process(proc)
    stdout, stderr, overflow = output
    if _cancel_requested(cancel_event):
        return ValidationResult(False, -1, stdout, stderr + f"{label} cancelled", False)
    if any(overflow):
        stderr += f"{label} output exceeds {output_cap} bytes"
        return ValidationResult(False, -1, stdout, stderr, False)
    return ValidationResult(proc.returncode == 0, proc.returncode,
                            stdout, stderr, False)


def _partial_output(exc) -> tuple[str, str]:
    """Join the bounded chunks collected before/after the kill."""
    return "".join(exc.stdout_lines), "".join(exc.stderr_lines)


def validate_timeout(timeout: float, label: str) -> None:
    """Reject unbounded or malformed deadlines before a subprocess can start."""
    try:
        valid = (not isinstance(timeout, bool) and isinstance(timeout, (int, float))
                 and timeout >= 0 and math.isfinite(timeout))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} timeout must be finite and non-negative")


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the POSIX command group; otherwise kill/reap only the direct child."""
    try:
        killpg = getattr(os, "killpg", None)
        if killpg is None:
            proc.kill()
        else:
            killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def read_chunks(stream, errors="replace"):
    """Read available pipe bytes without waiting for a newline or full chunk."""
    buffered = getattr(stream, "buffer", None)
    if buffered is None:
        yield from iter(lambda: stream.readline(8192), "")
        return
    decoder = codecs.getincrementaldecoder(stream.encoding or "utf-8")(errors)
    decoder = io.IncrementalNewlineDecoder(decoder, translate=True)
    for chunk in iter(lambda: buffered.read1(8192), b""):
        text = decoder.decode(chunk)
        if text:
            yield text
    tail = decoder.decode(b"", final=True)
    if tail:
        yield tail


def _read_stream(stream, collector: list, cap: int, overflow: list) -> None:
    """Drain a text pipe in fixed chunks while retaining only bounded data."""
    total = 0
    try:
        for line in read_chunks(stream):
            total += len(line.encode("utf-8", errors="replace"))
            if total <= cap:
                collector.append(line)
            else:
                overflow[0] = True
    except (OSError, ValueError) as exc:
        overflow.error = f"process output read failed: {exc}"
    finally:
        stream.close()


def _drain_process(proc: subprocess.Popen, start: float, timeout: float,
                   cap: int, cancel_event=None) -> tuple[str, str, tuple[bool, bool]]:
    """Drain both pipes against one absolute deadline."""
    stdout, stderr = [], []
    overflow = [StreamStatus(), StreamStatus()]
    threads = (
        threading.Thread(target=_read_stream,
                         args=(proc.stdout, stdout, cap, overflow[0]),
                         daemon=True),
        threading.Thread(target=_read_stream,
                         args=(proc.stderr, stderr, cap, overflow[1]),
                         daemon=True),
    )
    deadline = start + timeout
    try:
        for thread in threads:
            thread.start()
        while any(thread.is_alive() for thread in threads) or proc.poll() is None:
            if _cancel_requested(cancel_event):
                raise _CancelledDrain(stdout, stderr, threads)
            if any(value[0] or value.error for value in overflow):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _DrainTimeout(proc.args, timeout, stdout, stderr,
                                    (overflow[0][0], overflow[1][0]), threads)
            time.sleep(min(0.05, remaining))
    finally:
        _kill_group(proc)
        _join_drainers(threads)
    if error := next((value.error for value in overflow if value.error), ""):
        raise ValueError(error)
    return "".join(stdout), "".join(stderr), (
        overflow[0][0], overflow[1][0])


def _cancel_requested(cancel_event) -> bool:
    if shutdown_requested():
        return True
    try:
        return bool(cancel_event is not None and cancel_event.is_set())
    except (AttributeError, TypeError):
        return False


def _join_drainers(threads: tuple, maximum: float = 1.0) -> None:
    """Bound the post-kill wait while allowing partial pipe data to arrive."""
    deadline = time.monotonic() + max(0.0, maximum)
    for thread in threads:
        if thread.ident is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
