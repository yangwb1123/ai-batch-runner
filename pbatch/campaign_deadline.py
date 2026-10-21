"""Deadline refusals and exclusive, private isolated-attempt receipts."""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .campaign_budget import CampaignDeadlineExpired, CampaignGuardFailed, execution_deadline
from .campaign_watchdog import _create_json, lease_file


def deadline_error(deadline, direction, worktree: Path, pipeline: Path, logfile: Path,
                   *, phase: str, lease=None, child=None, stop_requested=False,
                   supervision_error_type=None, cleanup_error_type=None) -> CampaignDeadlineExpired:
    """Persist observed metadata, never claim effects rolled back or group containment."""
    evidence = [str(pipeline), str(logfile)]
    reason = "campaign deadline exhausted; worktree preserved; effects not rolled back"
    try:
        original = lease or {}
        payload = {
            "schema": 1, "status": "BUDGET_BLOCKED", "phase": phase,
            "direction_id": direction.direction_id,
            "worktree": str(worktree.resolve()), "pipeline": str(pipeline.resolve()),
            "log_file": str(logfile.resolve()), "observer_pid": os.getpid(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "cutoff_epoch": deadline.epoch,
            "attempt_monotonic_cutoff": deadline.monotonic,
            "child_pid": child.pid if child is not None else None,
            "child_start_ticks": (original.get("process_start_ticks") or
                                  (child.start_ticks if child is not None else None) or None),
            "child_returncode": child.returncode if child is not None else None,
            "group_stop_requested": bool(stop_requested),
            "effects_rolled_back": False,
            "supervision_error_type": supervision_error_type,
            "cleanup_error_type": cleanup_error_type,
        }
        lease_path = lease_file(worktree, direction.direction_id)
        receipt = lease_path.parent.parent / "deadline-receipts" / (
            lease_path.stem + "-" + uuid.uuid4().hex + ".json")
        _create_json(receipt, payload)
        evidence.append(str(receipt))
    except Exception as exc:
        # Persistence failure must not turn a budget stop into an ordinary
        # retryable failure or invent an empty successful receipt.
        reason += f"; deadline receipt unavailable: {type(exc).__name__}: {str(exc)[:256]}"
    return CampaignDeadlineExpired(reason, evidence)


def _primary_failure(error, child, cleanup_error):
    if not isinstance(error, Exception) and child.returncode is None:
        return CampaignGuardFailed("interrupted setup; child termination was not observed")
    if not isinstance(error, Exception) or isinstance(error, CampaignDeadlineExpired):
        if cleanup_error is not None and isinstance(error, CampaignDeadlineExpired):
            error.args = (str(error) + "; cleanup failed: " + type(cleanup_error).__name__,)
        return error
    return None


def supervision_failure(error, deadline, guard, child, direction, worktree, pipeline,
                        logfile, cleanup_error=None):
    """An observed budget refusal must survive metadata/cleanup failure."""
    primary = _primary_failure(error, child, cleanup_error)
    if primary is not None:
        return primary
    latched = bool(guard and guard.budget_exhausted)
    if not latched and isinstance(error, CampaignGuardFailed):
        return error
    try:
        expired = latched or (deadline is not None and deadline.expired(time.monotonic()))
    except Exception:
        return CampaignGuardFailed("campaign deadline state unavailable during supervision failure")
    if expired:
        return deadline_error(
            deadline, direction, worktree, pipeline, logfile, phase="supervision_error",
            child=child, stop_requested=guard.stop_requested if guard else True,
            supervision_error_type=type(error).__name__,
            cleanup_error_type=type(cleanup_error).__name__ if cleanup_error is not None else None)
    if cleanup_error is not None or child.returncode is None:
        return CampaignGuardFailed("isolated child termination unobserved after supervision failure")
    return error


def check_deadline(settings, direction, worktree: Path, pipeline: Path, logfile: Path,
                   *, phase: str) -> None:
    now = time.monotonic()
    deadline = execution_deadline(settings, now)
    refuse_expired(deadline, direction, worktree, pipeline, logfile, phase=phase)


def refuse_expired(deadline, direction, worktree: Path, pipeline: Path, logfile: Path,
                   *, phase: str) -> None:
    if deadline is not None and deadline.expired(time.monotonic()):
        raise deadline_error(deadline, direction, worktree, pipeline, logfile, phase=phase)
