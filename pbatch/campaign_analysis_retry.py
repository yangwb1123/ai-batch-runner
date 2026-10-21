"""Retry selection for failed analyses, bounded by the shared campaign cutoff."""

from __future__ import annotations

import time

from .campaign_budget import bound_task_timeout
from .campaign_state import failure_fingerprint
from .models import TaskResult
from .triage import is_retryable

ANALYSIS_BUDGET_REASON = ("campaign deadline exhausted before this analysis started; "
                          "no model call was dispatched")


def deadline_admission(deadline):
    """Worker-boundary admission: refuse a queued analysis with no window left."""
    if deadline is None:
        return None

    def admit(task, started_at=None):
        if bound_task_timeout(task, deadline, started_at):
            return None
        return TaskResult(task=task, success=False, elapsed=0.0,
                          reason=ANALYSIS_BUDGET_REASON + "; queued task was not started")

    admit.rate_timeout = lambda: max(0.0, deadline.remaining(time.monotonic()))
    return admit


def retryable_tasks(results, prior_failures: dict, retries_left: bool) -> list:
    """Failed tasks worth retrying now; unchanged repeat failures are skipped."""
    if not retries_left:
        return []
    from .provider_breaker import is_provider_failure_reason
    pending = []
    for result in results:
        if result.success or not is_retryable(result.reason):
            continue
        if is_provider_failure_reason(result.reason):
            continue
        key = str(result.task.output_path())
        signature = failure_fingerprint("", "ANALYSIS", result.reason)
        if prior_failures.get(key) == signature:
            continue
        prior_failures[key] = signature
        pending.append(result.task)
    return pending
