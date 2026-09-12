"""Bounded, descriptive efficiency metrics for campaign rounds."""

from __future__ import annotations

import math

from .campaign_strategy import UsageSummary, _read_rows


def execution_attempts(path: str, run_id: str = "") -> tuple[int, int]:
    """Return (execution attempts, retry attempts) from bounded metering rows."""
    if not path:
        return 0, 0
    rows, _ = _read_rows(path)
    attempts = 0
    retries = 0
    for row in rows:
        if run_id and row.get("run_id") != run_id:
            continue
        if row.get("event") not in {"task_finish", "task_fail"}:
            continue
        attempts += 1
        try:
            attempt = int(row.get("attempt", 0) or 0)
        except (TypeError, ValueError):
            attempt = 0
        retries += int(attempt > 1)
    return attempts, retries


def build_efficiency_metrics(outcomes: list, wall_seconds: float,
                             concurrency: int, usage: UsageSummary,
                             *, attempts: int = 0, retries: int = 0) -> dict:
    """Build a JSON-safe metric snapshot; values are observations, not grants."""
    total = len(outcomes)
    passed = sum(1 for item in outcomes if item.status == "PASSED")
    gate_rejected = sum(1 for item in outcomes if item.status == "GATE_REJECTED")
    validation_failed = sum(
        1 for item in outcomes if item.status == "VALIDATION_FAILED")
    pipeline_failed = sum(1 for item in outcomes if item.status == "PIPELINE_FAILED")
    work_seconds = sum(max(0.0, _number(getattr(item, "elapsed", 0)))
                         for item in outcomes)
    wall = max(0.0, _number(wall_seconds))
    slots = max(1, int(concurrency or 1))
    utilization = min(1.0, work_seconds / (wall * slots)) if wall else 0.0
    total_tokens = max(0, int(usage.total_tokens or 0))
    attempt_count = max(0, int(attempts))
    retry_count = min(attempt_count, max(0, int(retries)))
    return {
        "directions_attempted": total,
        "directions_passed": passed,
        "directions_gate_rejected": gate_rejected,
        "directions_validation_failed": validation_failed,
        "directions_pipeline_failed": pipeline_failed,
        "wall_seconds": round(wall, 3),
        "pipeline_work_seconds": round(work_seconds, 3),
        "concurrency_slots": slots,
        "concurrency_slot_utilization": round(utilization, 6),
        "analysis_calls": max(0, int(usage.calls or 0)),
        "analysis_tokens": total_tokens,
        "analysis_tokens_per_successful_direction": (
            round(total_tokens / passed, 3) if passed else None),
        "execution_attempts": attempt_count,
        "retry_attempts": retry_count,
        "retry_ratio": round(retry_count / attempt_count, 6) if attempt_count else 0.0,
        "gate_rejection_ratio": round(gate_rejected / total, 6) if total else 0.0,
        "no_progress_seconds": round(sum(
            max(0.0, _number(getattr(item, "no_progress_seconds", 0)))
            for item in outcomes), 3),
    }


def _number(value: object) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value >= 0 else 0.0
