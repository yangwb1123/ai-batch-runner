"""Bounded live subprocess draining for the task runner."""

from __future__ import annotations

import sys
import threading
import time

from .runner_cancel import cancel_requested
from .process_exec import StreamStatus


def drain_process(proc, prefix, start, timeout, cap, emit,
                  stdout_target, stderr_target, stderr_emit, cancel_event,
                  read_stream, kill_group, poll_wait, timeout_type,
                  cancelled_type, join_threads):
    stdout_lines, stderr_lines = [], []
    stdout_overflow, stderr_overflow = StreamStatus(), StreamStatus()
    stdout_sink = stdout_target or sys.stdout
    stderr_sink = stderr_target or sys.stderr
    out_args = (cap, stdout_overflow, emit, stdout_sink)
    err_args = (cap, stderr_overflow,
                emit if stderr_emit is None else stderr_emit, stderr_sink)
    tout = threading.Thread(
        target=read_stream, args=(proc.stdout, prefix, stdout_lines, *out_args),
        daemon=True)
    terr = threading.Thread(
        target=read_stream, args=(proc.stderr, prefix, stderr_lines, *err_args),
        daemon=True)
    deadline = start + timeout
    try:
        tout.start()
        terr.start()
        while tout.is_alive() or terr.is_alive() or proc.poll() is None:
            if cancel_requested(cancel_event):
                raise cancelled_type(stdout_lines, stderr_lines, (tout, terr))
            if (stdout_overflow[0] or stderr_overflow[0]
                    or stdout_overflow.error or stderr_overflow.error):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_type(proc.args, timeout, stdout_lines, stderr_lines,
                                   (stdout_overflow[0], stderr_overflow[0]),
                                   (tout, terr))
            poll_wait.wait(min(0.05, remaining))
    finally:
        kill_group(proc)
        join_threads(tuple(thread for thread in (tout, terr)
                           if thread.ident is not None))
    return stdout_lines, stderr_lines, stdout_overflow, stderr_overflow
