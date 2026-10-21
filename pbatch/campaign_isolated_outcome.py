"""Private attempt context and fail-closed diagnostic publication."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .campaign_budget import CampaignDeadlineExpired, CampaignGuardFailed
from .campaign_models import CampaignSettings, Direction
from .campaign_recovery import RECOVERY_PRESERVED


@dataclass(frozen=True)
class IsolatedAttempt:
    settings: CampaignSettings
    direction: Direction
    worktree: Path
    branch: str
    baseline: str
    pipeline: Path
    logfile: Path
    started: float
    start_clean: bool
    recovery_root: Path


def parent_failure(error, deadline, worktree):
    """Parent I/O may fail late, but cannot grant another budget or ordinary retry."""
    if isinstance(error, CampaignDeadlineExpired) or deadline is None:
        return error
    try:
        expired = deadline.expired(time.monotonic())
    except Exception:
        return CampaignGuardFailed("deadline state unavailable after " + type(error).__name__)
    if not expired:
        return error
    refusal = CampaignDeadlineExpired(
        "campaign deadline exhausted during parent I/O; effects not rolled back; "
        + type(error).__name__ + ": " + str(error)[:256]
        + "; deadline receipt unavailable for interrupted parent operation", [str(worktree)])
    refusal.__cause__ = error
    return refusal


def check_publication(outcome, deadline, worktree):
    """One boundary for every worker return, including pre-child refusals."""
    if deadline is not None and deadline.expired(time.monotonic()):
        raise CampaignDeadlineExpired(
            "campaign deadline exhausted before outcome publication; effects not rolled back; "
            + outcome.reason + "; deadline receipt unavailable for early worker return",
            [str(worktree), *outcome.evidence])


def preparation_outcome(factory, error, direction, analysis, settings, root, started):
    error = parent_failure(error, getattr(settings, "_execution_deadline", None), root)
    budget = isinstance(error, CampaignDeadlineExpired)
    preserve = budget or isinstance(error, CampaignGuardFailed)
    return factory(direction, "BUDGET_BLOCKED" if budget else "PIPELINE_FAILED", str(error),
                   time.monotonic() - started if preserve else 0, [str(analysis)],
                   recovery_status=RECOVERY_PRESERVED if preserve else "",
                   recovery_reason="preparation interrupted; worktree identity unavailable" if preserve else "")


def stopped_outcome(factory, error, direction, worktree, branch, baseline, started, probe,
                    deadline=None):
    """Classify the refusal before optional diagnostics; diagnostics cannot erase it."""
    error = parent_failure(error, deadline, worktree)
    diagnostic_note = ""
    try:
        repository = probe(worktree)
    except Exception as diagnostic:
        unavailable = "Git evidence unavailable: " + type(diagnostic).__name__
        diagnostic_note = "; " + unavailable
        repository = {"head_commit": "", "clean_commit": "", "dirty_digest": "",
                      "dirty_evidence": [unavailable]}
    error = parent_failure(error, deadline, worktree)
    budget = isinstance(error, CampaignDeadlineExpired)
    preserve = budget or isinstance(error, CampaignGuardFailed) or bool(diagnostic_note)
    reason = str(error) + diagnostic_note
    evidence = [str(worktree), *(error.evidence if budget else [])]
    return factory(
        direction, "BUDGET_BLOCKED" if budget else "PIPELINE_FAILED", reason,
        time.monotonic() - started if preserve else 0, evidence, branch,
        repository["head_commit"], baseline, repository["clean_commit"],
        repository["dirty_evidence"], repository["dirty_digest"],
        recovery_status=RECOVERY_PRESERVED if preserve else "",
        recovery_reason="execution supervision stop; no further automatic recovery" if preserve else "")
