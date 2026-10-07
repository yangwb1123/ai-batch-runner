"""Read-only request receipts. Matching observed pins never mean ready to execute."""
from __future__ import annotations

from pathlib import Path

from .task_center_io import checked_path, display
from .task_consumption import ConsumptionIndex
from .task_repair_assessment import assess_request
from .task_repair_binding import read_json
from .task_repair_schema import MAX_REQUESTS, deadline, fingerprint, validate_state
from .task_repair_store import request_path, request_root


def repair_views(sources: list, goals: list, candidates: list, now: float,
                 consumption: ConsumptionIndex | None = None) -> tuple[list, list]:
    selected = {source["source_id"]: source for source in sources}
    goal_map = {goal["goal_id"]: goal for goal in goals}
    checkpoints = {"tc-" + key: (rows[-1]["checkpoint"], partial)
                   for _, key, _, rows, partial in candidates if rows[-1]["kind"] == "pipeline"}
    results, notes = [], []
    inspection_index = consumption if consumption is not None else ConsumptionIndex(candidates, sources)
    try:
        root = checked_path(request_root())
        if not root.is_dir():
            return results, notes
        for index, path in enumerate(root.iterdir()):
            if index >= MAX_REQUESTS * 2 + 16 or len(results) >= MAX_REQUESTS:
                notes.append("repair request discovery window limited")
                break
            if path.suffix == ".json":
                _read(path, selected, goal_map, checkpoints, inspection_index, now, results, notes)
    except (OSError, ValueError) as exc:
        notes.append("repair requests unavailable: " + display(str(exc)))
    return results, notes


def _read(path: Path, sources: dict, goals: dict, checkpoints: dict, index: ConsumptionIndex,
          now: float, results: list, notes: list) -> None:
    try:
        value = read_json(path)
        if value is None:
            return
        validate_state(value)
        if path != request_path(value["repair_id"]):
            raise ValueError("repair file identity mismatch")
        source = sources.get(value["manifest"]["source_id"])
        if source is not None:
            record = _public(value, source, goals, checkpoints, now)
            manifest = value["manifest"]
            lineage = index.inspection({(manifest["source_id"], manifest["run_id"])})
            record["assessment"] = assess_request(value, goals.get(manifest["goal_id"]),
                checkpoints.get(manifest["task_id"]), lineage, now)
            results.append(record)
    except (OSError, ValueError, TypeError) as exc:
        notes.append("repair request invalid: " + display(str(exc)))


def _status(value: dict, goal: dict | None, observed: tuple | None, now: float) -> str:
    manifest = value["manifest"]
    if value["state"] == "withdrawn":
        return "withdrawn"
    if deadline(manifest["limits"]["requested_deadline_at"]) <= now:
        return "expired_declaration"
    if goal is None:
        return "goal_unavailable_in_view"
    if (goal["source_id"] != manifest["source_id"] or goal["version"] != manifest["goal_version"]
            or goal["contract_digest"] != manifest["contract_digest"]):
        return "stale_goal_revision"
    if manifest["run_id"] not in {run["run_id"] for run in goal["runs"]}:
        return "goal_binding_unavailable_in_view"
    return _checkpoint_status(manifest, goal, observed)


def _checkpoint_status(manifest: dict, goal: dict, observed: tuple | None) -> str:
    if observed is None:
        return "checkpoint_unavailable_in_view"
    checkpoint, partial = observed
    if partial:
        return "partial_observation_window"
    if manifest["checkpoint"] != {"sequence": checkpoint["seq"], "sha256": fingerprint(checkpoint)}:
        return "stale_observed_checkpoint"
    copies = [run["checkpoint"] for run in goal["runs"] if run["run_id"] == manifest["run_id"]]
    if any(copy is None for copy in copies):
        return "durable_checkpoint_header_unavailable_in_goal_view"
    if any((copy["status"], copy["sequence"], copy["last_observed_at"]) !=
           (checkpoint["status"], checkpoint["seq"], checkpoint["ts"]) for copy in copies):
        return "checkpoint_header_conflict_with_goal_view"
    return "declared_request_only"


def _public(value: dict, source: dict, goals: dict, checkpoints: dict, now: float) -> dict:
    manifest = value["manifest"]
    return {"repair_id": value["repair_id"], "source_id": manifest["source_id"], "project": source["name"],
        "task_id": manifest["task_id"], "run_id": manifest["run_id"], "goal_id": manifest["goal_id"],
        "version": value["version"], "state": value["state"],
        "status": _status(value, goals.get(manifest["goal_id"]), checkpoints.get(manifest["task_id"]), now),
        "goal_version": manifest["goal_version"], "contract_digest": manifest["contract_digest"],
        "checkpoint": manifest["checkpoint"], "stage_key": manifest["stage_key"],
        "receipt": {**value["receipt"], "stage_label": display(value["receipt"]["stage_label"], 160),
                    "basis": "goal_and_checkpoint_fields_rechecked_at_registration_only_not_live_preflight"},
        "limits": manifest["limits"], "limits_basis": "operator_declared_not_original_budget_verification_or_enforcement",
        "original_limits_verified": False, "execution_authority": "not_granted",
        "audit": [{**row, "by": display(row["by"], 128), "reason": display(row["reason"], 1024)} for row in value["audit"]],
        "actor_identity": "unauthenticated_local_label",
        "capabilities": {"control": False, "cleanup": False, "feedback_upload": False,
                         "budget_or_deadline_reset": False, "provider_breaker_reset": False}}


def attach_repairs(tasks: list, goals: list, repairs: list) -> None:
    for task in tasks:
        task["repair_links"] = [row["repair_id"] for row in repairs if row["task_id"] == task["task_id"]]
    for goal in goals:
        goal["repair_links"] = [row["repair_id"] for row in repairs if row["goal_id"] == goal["goal_id"]]
