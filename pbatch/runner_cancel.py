"""Scoped cancellation primitives shared by the bounded task runner."""

from __future__ import annotations

import time
from typing import Any

from . import ratelimit
from .config import log
from .models import Task, TaskResult
from .process_shutdown import shutdown_requested


class CancelledDrain(RuntimeError):
    """A task process was killed by its scoped cancellation event."""

    def __init__(self, stdout_lines, stderr_lines, threads):
        self.stdout_lines = stdout_lines
        self.stderr_lines = stderr_lines
        self.threads = threads
        super().__init__("task cancelled")


def cancel_requested(cancel_event: Any) -> bool:
    if shutdown_requested():
        return True
    try:
        return bool(cancel_event is not None and cancel_event.is_set())
    except (AttributeError, TypeError):
        return False


def task_cancel_event(cancel_event: Any, task: Any) -> Any:
    """Resolve an optional task-scoped event without changing old callers."""
    resolver = getattr(cancel_event, "for_task", None)
    if not callable(resolver):
        return cancel_event
    try:
        resolved = resolver(task)
    except (AttributeError, TypeError, ValueError):
        return cancel_event
    return resolved if resolved is not None else cancel_event


def join_stream_threads(threads: tuple, maximum: float = 1.0) -> None:
    deadline = time.monotonic() + max(0.0, maximum)
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))


def wait_for_cancel(seconds: float, cancel_event: Any, sleep) -> bool:
    if cancel_event is None:
        sleep(seconds)
        return True
    deadline = time.monotonic() + max(0.0, seconds)
    while not cancel_requested(cancel_event):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        try:
            if cancel_event.wait(min(0.25, remaining)):
                return False
        except (AttributeError, TypeError):
            sleep(remaining)
            return not cancel_requested(cancel_event)
    return False


def rate_gate(task, cancel_event=None) -> bool:
    """Wait for provider admission but never spawn after cancellation."""
    if cancel_requested(cancel_event):
        return False
    if ratelimit.RATE_PER_SECOND <= 0 and not ratelimit.PROVIDER_RATES:
        return True
    if cancel_event is None:
        accepted = ratelimit.acquire(task.provider or "", timeout=300.0)
    else:
        accepted = ratelimit.acquire(
            task.provider or "", timeout=300.0, cancel_event=cancel_event)
    if not accepted:
        if cancel_requested(cancel_event):
            return False
        log.warning("RATE: token wait timed out for provider '%s' (300s); spawning anyway",
                    task.provider or "default")
    return True


def cancelled_result(task: Task) -> TaskResult:
    return TaskResult(task=task, success=False, returncode=-1,
                      reason="cancelled")
