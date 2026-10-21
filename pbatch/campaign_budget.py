"""Campaign budget and deadline that survive a supervisor restart.

A 7x24 campaign is normally driven by a supervisor loop, and the natural
implementation of that loop restarts the campaign process.  If the budget is
measured from *process* start, every restart silently extends the run — the
campaign then lives longer than the operator ever authorised, and nothing in
the logs says so.

Two origins are supported, both anchored outside the process:

* ``budget_seconds`` — measured from the first ``CAMPAIGN_RUN_STARTED`` row in
  the campaign state ledger, i.e. from the first time this campaign ever ran;
* ``deadline_at`` — an absolute ISO-8601 timestamp or epoch seconds, for an
  externally enforced wall-clock cutoff.

Both are opt-in; with neither configured the behaviour is unchanged.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field, replace
from time import monotonic
from datetime import datetime, timezone
from pathlib import Path

_RUN_STARTED = "CAMPAIGN_RUN_STARTED"
# Bounded: the origin row is found by scanning a bounded tail of the ledger.
_MAX_SCAN_BYTES = 4 * 1024 * 1024


def origin_epoch(state_path: Path | None) -> float | None:
    """First recorded campaign start, or None when the ledger has no row."""
    if state_path is None or not state_path.is_file():
        return None
    try:
        with state_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if _RUN_STARTED not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("status") != _RUN_STARTED:
                    continue
                stamp = _parse_time(row.get("updated_at"))
                if stamp is not None:
                    return stamp
    except OSError:
        return None
    return None


def _parse_time(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def parse_deadline(value: str) -> float | None:
    """Accept ISO-8601 or epoch seconds; reject anything else fail-closed."""
    if not str(value or "").strip():
        return None
    try:
        stamp = _parse_time(str(value).strip())
        return stamp if stamp is not None and math.isfinite(stamp) else None
    except (OSError, ValueError, OverflowError):
        return None


def deadline_for(budget_seconds: int, deadline_at: str,
                 state_path: Path | None) -> tuple[float | None, str]:
    """Resolve the campaign cutoff and the origin label used for logging."""
    absolute = parse_deadline(deadline_at)
    if absolute is not None:
        return absolute, "absolute deadline"
    if budget_seconds and budget_seconds > 0:
        origin = origin_epoch(state_path)
        if origin is not None:
            return origin + budget_seconds, "first recorded campaign start"
        return time.time() + budget_seconds, "this process start (no ledger origin yet)"
    return None, ""


def expired(budget_seconds: int, deadline_at: str,
            state_path: Path | None) -> tuple[bool, str]:
    """(is_expired, human reason). Never raises: a stop decision, not a crash."""
    try:
        cutoff, origin = deadline_for(budget_seconds, deadline_at, state_path)
        if cutoff is None:
            return False, ""
        remaining = cutoff - time.time()
        if remaining > 0:
            return False, (f"campaign budget has {int(remaining // 60)} min left "
                           f"(origin: {origin})")
        return True, (f"campaign budget exhausted (origin: {origin}, "
                      f"cutoff {datetime.fromtimestamp(cutoff, timezone.utc).isoformat()})")
    except (OSError, ValueError, OverflowError):
        return False, ""


def state_path_for(root: Path, settings) -> Path:
    """The ledger a campaign actually writes to (config or private default)."""
    try:
        return settings.state_path(root)
    except (AttributeError, OSError, ValueError):
        return Path(root) / "docs" / "auto" / "state.jsonl"


BUDGET_BLOCKED_REASON = ("campaign budget or deadline expired before this "
                         "direction started")


def gate(settings, root: Path):
    """Return a callable reporting whether new campaign work may start.

    Resolve once against the coordinator ledger, not a worktree's private
    runtime. Re-check the clock before preparation and again after queueing.
    A missing ledger's fallback must not slide forward on every check. This
    admission gate does not interrupt work already running.
    """
    frozen = getattr(settings, "_execution_deadline", None)
    if frozen is None:
        cutoff, _origin = deadline_for(
            settings.budget_seconds, settings.deadline_at, state_path_for(root, settings))
        frozen = _clock_cap(cutoff) if cutoff is not None else None

    def allowed() -> bool:
        return frozen is None or not frozen.expired(monotonic())

    return allowed


class CampaignDeadlineExpired(RuntimeError):
    """An isolated attempt stopped for budget, not an ordinary pipeline failure."""

    def __init__(self, reason: str, evidence: list[str]):
        super().__init__(reason)
        self.evidence = list(evidence)


class CampaignGuardFailed(RuntimeError):
    """Required execution supervision is unavailable; preserve effects, never retry blindly."""


@dataclass(frozen=True)
class ExecutionDeadline:
    epoch: float
    monotonic: float
    _exhausted: threading.Event = field(default_factory=threading.Event, compare=False, repr=False)

    def expired(self, now: float) -> bool:
        if self._exhausted.is_set():
            return True
        if now >= self.monotonic or time.time() >= self.epoch:
            self._exhausted.set()
        return self._exhausted.is_set()

    def remaining(self, now: float) -> float:
        if self.expired(now):
            return 0.0
        window = min(self.monotonic - now, self.epoch - time.time())
        if window <= 0:
            self._exhausted.set()  # an observed empty window is terminal
            return 0.0
        return window


def _clock_cap(epoch: float) -> ExecutionDeadline:
    return ExecutionDeadline(epoch, monotonic() + (epoch - time.time()))


def with_frozen_deadline(settings, root: Path):
    """Carry one coordinator cutoff to worktrees without rereading their ledgers."""
    if getattr(settings, "_execution_deadline", None) is not None:
        return settings
    if settings.deadline_at and parse_deadline(settings.deadline_at) is None:
        raise ValueError("invalid campaign deadline")
    cutoff, _origin = deadline_for(
        settings.budget_seconds, settings.deadline_at, state_path_for(root, settings))
    if cutoff is None:
        return settings
    if not math.isfinite(cutoff):
        raise ValueError("invalid coordinator campaign deadline")
    frozen = replace(settings, deadline_at=str(cutoff))
    frozen._execution_deadline = _clock_cap(cutoff)
    return frozen


def execution_settings(settings, started: float):
    """Private runtime copy: share one clock/latch through parent and child paths."""
    deadline = execution_deadline(settings, started)
    if deadline is None or getattr(settings, "_execution_deadline", None) is not None:
        return settings
    frozen = replace(settings)
    frozen._execution_deadline = deadline
    return frozen


def context_settings(context):
    """Freeze one coordinator cutoff; analysis, queues and rounds share it."""
    settings = getattr(context, "deadline_settings", None)
    if settings is None:
        settings = with_frozen_deadline(context.settings, context.root)
        context.deadline_settings = settings
    return settings


def context_deadline(context):
    """The shared batch clock/latch, or None when no budget is configured."""
    return getattr(context_settings(context), "_execution_deadline", None)


def bound_task_timeout(task, deadline, started_at: float | None = None) -> bool:
    """Cap one task's timeout by the remaining batch window; False = expired.

    ``started_at`` is the runner's monotonic run start. The task timeout is
    measured from there, so a re-admission after preparation/rate/lock waits
    must add the already-consumed interval instead of resetting the window to
    the time left at the admission moment.
    """
    if deadline is None:
        return True
    now = time.monotonic()
    if deadline.expired(now):
        return False
    window = deadline.remaining(now)
    if window <= 0:
        return False  # observed zero/negative window is refusal, never a credit
    if started_at is not None:
        window += max(0.0, now - started_at)
    cap = max(1, math.ceil(window))
    task.timeout = min(task.timeout or cap, cap)
    return True


def retry_delay_before_cutoff(deadline, delay: float) -> float | None:
    """Sleep duration before a retry, or None when the cutoff already passed."""
    if deadline is None:
        return max(0.0, delay)
    remaining = deadline.remaining(time.monotonic())
    if remaining <= 0:
        return None
    return min(max(0.0, delay), remaining)


def execution_deadline(settings, started: float) -> ExecutionDeadline | None:
    """Execution accepts only absolute cutoffs; relative budgets need a coordinator."""
    frozen = getattr(settings, "_execution_deadline", None)
    if frozen is not None:
        return frozen
    raw = getattr(settings, "deadline_at", "")
    if not raw:
        if getattr(settings, "budget_seconds", 0):
            raise ValueError("campaign deadline requires a frozen coordinator cutoff")
        return None
    epoch = parse_deadline(raw)
    if epoch is None:
        raise ValueError("invalid campaign deadline")
    # Anchor before spawn/lease setup; wall rollback cannot extend this attempt.
    return ExecutionDeadline(epoch, started + (epoch - time.time()))
