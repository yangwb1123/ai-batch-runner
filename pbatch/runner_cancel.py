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


class CombinedCancel:
    """OR of two cancellation sources, preserving task-scoped routing.

    A campaign cutoff event and a native approval channel must both be able
    to stop work: preferring one silently would let the other's accepted
    cancellation request leave an in-flight task running.
    """

    def __init__(self, events: list):
        self._events = [event for event in events if event is not None]

    def is_set(self) -> bool:
        return any(cancel_requested(event) for event in self._events)

    def wait(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            if self.is_set():
                return True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.05, remaining))
            else:
                time.sleep(0.05)

    def for_task(self, task: Any) -> Any:
        return CombinedCancel([task_cancel_event(event, task) for event in self._events])

    def set(self) -> None:
        for event in self._events:
            setter = getattr(event, "set", None)
            if callable(setter):
                setter()


def compose_cancel(*events: Any) -> Any:
    """Combine cancellation sources without dropping per-task routing."""
    present = [event for event in events if event is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return CombinedCancel(present)


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


def rate_gate(task, cancel_event=None, timeout=None) -> bool:
    """Wait for provider admission but never spawn after cancellation.

    ``timeout`` bounds the wait by a caller-owned window (campaign cutoff);
    the historical 300s ceiling still applies.
    """
    if cancel_requested(cancel_event):
        return False
    if ratelimit.RATE_PER_SECOND <= 0 and not ratelimit.PROVIDER_RATES:
        return True
    wait = 300.0 if timeout is None else min(300.0, max(0.0, timeout))
    if cancel_event is None:
        accepted = ratelimit.acquire(task.provider or "", timeout=wait)
    else:
        accepted = ratelimit.acquire(
            task.provider or "", timeout=wait, cancel_event=cancel_event)
    if not accepted:
        if cancel_requested(cancel_event):
            return False
        log.warning("RATE: token wait timed out for provider '%s' (%.0fs); spawning anyway",
                    task.provider or "default", wait)
    return True


def rate_bounded_admission(task, cancel_event, admit, cancelled, gate,
                           started_at=None) -> Any:
    """Bound the rate wait, then re-admit at the true pre-spawn boundary.

    Returns a refusal TaskResult (no spawn) or None when the caller may spawn.
    ``started_at`` is the task's monotonic run start so the deadline check can
    account for time already spent before this boundary.
    """
    limit = getattr(admit, "rate_timeout", None) if admit is not None else None
    timeout = limit() if limit is not None else None
    if gate(task, cancel_event, timeout) is False or cancel_requested(cancel_event):
        return cancelled(task)
    return admit(task, started_at) if admit is not None else None


def cancelled_result(task: Task) -> TaskResult:
    return TaskResult(task=task, success=False, returncode=-1,
                      reason="cancelled")
