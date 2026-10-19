"""Verified mapping of recorded CLI spawn/outcome observations to exact task identities.

The window is whatever the caller already admitted (the task-center window); these
functions never discover feeds, probe processes or infer identities from titles,
PIDs or timestamps.  A recorded outcome without a spawn observation keeps its
unverified provenance: it is counted, never treated as a verified execution, and
never converted into zero.
"""
from __future__ import annotations

METHOD = "verified-execution-mapping-v1"
MAX_REFS = 128
MAX_UNVERIFIED_REFS = 8
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timeout", "unknown"})


def execution_mapping(rows: list, *, partial: bool = False,
                      limit: int = MAX_REFS) -> dict:
    """Classify admitted execution observations by exact recorded identity.

    ``partial`` marks coverage gaps (truncated window, malformed rows, read
    errors): the counts are then lower bounds and ``unknown_not_zero`` is set.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("execution mapping limit must be a positive integer")
    if not isinstance(rows, (list, tuple)):
        raise ValueError("execution mapping requires a list of observation rows")
    malformed = sum(1 for row in rows if not isinstance(row, dict))
    executions = [row for row in rows if isinstance(row, dict)
                  and row.get("kind") == "agent" and row.get("phase") == "execution"]
    verified, spawn_only, outcome_without_spawn, legacy = _classify(executions)
    refs = [_ref(row) for row in verified]
    return {
        "method": METHOD,
        "scope": "admitted_task_center_window_only",
        "basis": "recorded_spawn_and_outcome_observations_matched_by_exact_task_identity",
        "execution_observations": len(executions),
        "verified_spawn_and_outcome": len(verified),
        "spawn_only_active_observations": len(spawn_only),
        "outcome_without_spawn_observation": len(outcome_without_spawn),
        "legacy_identity_observations": len(legacy),
        "other_phase_observations": len(rows) - len(executions) - malformed,
        "malformed_observations": malformed,
        "verified_refs": refs[:limit],
        "omitted_verified_refs": max(0, len(refs) - limit),
        "unverified_outcome_refs": [_ref(row) for row in outcome_without_spawn[:MAX_UNVERIFIED_REFS]],
        "coverage": "partial_window" if partial else "observed_rows_only",
        "unknown_not_zero": bool(partial),
        "identity_basis": "exact_run_task_attempt_phase_fields_only",
        "title_pid_timestamp_inference": "not_performed",
        "liveness": "not_verified",
        "ownership": "not_verified",
        "acceptance": "not_performed",
        "note": ("spawn_only rows are observations, not liveness; an outcome without a spawn "
                 "observation has unverified provenance; window counts must not be added "
                 "to other windows"),
    }


def _classify(executions: list) -> tuple:
    verified, spawn_only, outcome_without_spawn, legacy = [], [], [], []
    for row in executions:
        spawn, status = row.get("spawn_observed"), str(row.get("status") or "")
        if row.get("legacy_identity"):
            legacy.append(row)
        elif spawn is True and status in TERMINAL_STATUSES:
            verified.append(row)
        elif spawn is True:
            spawn_only.append(row)
        elif status in TERMINAL_STATUSES:
            outcome_without_spawn.append(row)
        else:
            spawn_only.append(row)
    return verified, spawn_only, outcome_without_spawn, legacy


def _ref(row: dict) -> dict:
    return {"record_ref": row.get("record_ref"),
            "run_id": row.get("run_id") or None,
            "task_id": row.get("local_task_id") or None,
            "attempt": row.get("attempt"),
            "stage": row.get("stage") or None,
            "role": row.get("role") or None,
            "status": row.get("status") or None,
            "started_at": row.get("started_at"),
            "ended_at": row.get("ts"),
            "spawn_observed": row.get("spawn_observed")}
