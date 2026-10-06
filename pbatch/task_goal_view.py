"""Read-only confirmed metadata and evidence-bound claims, shared by all clients."""
from __future__ import annotations

from pathlib import Path

from .task_center_io import checked_path, display
from .task_goal_schema import validate_state
from .task_goal_store import (MAX_GOALS, evidence_reference, goal_path)
from .task_lifecycle import load_run
from .task_plan_view import plan_flow
from .task_state_io import load
from .user_dirs import pbatch_runtime_home

MAX_HASH_BYTES = 32 * 1024 * 1024


class EvidenceWindow:
    def __init__(self):
        self.remaining = MAX_HASH_BYTES
        self.cache = {}

    def verify(self, source: dict, reference: dict) -> str:
        key = (source["source_id"], reference["path"], reference["sha256"], reference["bytes"])
        if key in self.cache:
            return self.cache[key]
        try:
            actual = evidence_reference(source, reference["path"],
                min(self.remaining, 16 * 1024 * 1024), self.consume)
            status = "matching" if actual == reference else "changed"
        except FileNotFoundError:
            status = "missing"
        except (OSError, ValueError):
            status = "unavailable"
        self.cache[key] = status
        return status

    def consume(self, amount: int) -> None:
        self.remaining = max(0, self.remaining - amount)


def goal_views(sources: list[dict]) -> tuple[list[dict], list[str]]:
    selected = {source["source_id"]: source for source in sources}
    root = pbatch_runtime_home() / "ops" / "goals"
    results, notes, window = [], [], EvidenceWindow()
    try:
        root = checked_path(root)
        if not root.is_dir():
            return results, notes
        for index, path in enumerate(root.iterdir()):
            if index >= MAX_GOALS * 4 or len(results) >= MAX_GOALS:
                notes.append("goal metadata discovery window limited")
                break
            if path.suffix == ".json":
                _read_goal(path, selected, results, notes, window)
    except (OSError, ValueError) as exc:
        notes.append("goal metadata unavailable: " + display(str(exc)))
    return results, notes


def _read_goal(path: Path, sources: dict, results: list, notes: list, window) -> None:
    try:
        value = load(path)
        if value is None:
            return
        validate_state(value)
        if goal_path(value["goal_key"]) != path:
            raise ValueError("goal path/identity mismatch")
        if value["source_id"] in sources:
            results.append(_project(value, sources[value["source_id"]], window))
    except (OSError, ValueError, TypeError) as exc:
        notes.append("goal metadata invalid: " + display(str(exc)))


def _project(value: dict, source: dict, window: EvidenceWindow) -> dict:
    latest = {row["criterion_id"]: row for row in value["attestations"]
              if row["contract_digest"] == value["contract_digest"]}
    criteria = [_criterion(item, latest.get(item["id"]), source, window)
                for item in value["contract"]["criteria"]]
    acceptance = _acceptance(criteria)
    runs = [_run(source, item["run_id"]) for item in value["bindings"]]
    observed = [row for row in runs if row["checkpoint"] is not None]
    current = max(observed, key=lambda row: row["checkpoint"]["ts"], default=None)
    return {"goal_id": value["goal_key"], "logical_goal_id": value["contract"]["goal_id"],
            "source_id": source["source_id"], "project": source["name"],
            "statement": display(value["contract"]["statement"], 4096), "version": value["version"],
            "contract_revision": value["contract_revision"], "contract_digest": value["contract_digest"],
            "origin": "explicit_cli_metadata", "reviewer_identity": "not_authenticated",
            "criteria": criteria, "acceptance": acceptance, "semantic_verification": "not_performed",
            "progress": {"criteria": len(criteria), "matching_attestations": sum(
                row["state"] == "operator_attested" for row in criteria), "goal_percent": None},
            "runs": [_public_run(row) for row in runs],
            "flow": plan_flow(current["checkpoint"]) if current else None,
            "history": [_history(row) for row in value["history"][-32:]],
            "efficiency": {"tokens_per_accepted_goal": None, "cost_per_accepted_goal": None,
                "note": "child accounting is windowed; attestation is not independent quality verification"},
            "capabilities": {"control": False, "cleanup": False, "feedback_upload": False}}


def _criterion(item: dict, receipt: dict | None, source: dict, window) -> dict:
    result = {"id": item["id"], "description": display(item["description"], 1024),
              "state": "pending", "by": None, "reason": None, "evidence": None}
    if receipt is None:
        return result
    result.update(by=display(receipt["by"], 128), reason=display(receipt["reason"], 1024))
    if receipt["decision"] == "rejected":
        result["state"] = "operator_rejected"
        return result
    proof = window.verify(source, receipt["evidence"])
    result.update(state="operator_attested" if proof == "matching" else "evidence_" + proof,
                  evidence={"path": display(receipt["evidence"]["path"], 4096),
                            "sha256": receipt["evidence"]["sha256"], "status": proof})
    return result


def _acceptance(criteria: list) -> str:
    if any(row["state"] == "operator_rejected" for row in criteria):
        return "operator_rejected"
    if all(row["state"] == "operator_attested" for row in criteria):
        return "operator_attested"
    if any(row["state"].startswith("evidence_") for row in criteria):
        return "evidence_not_current"
    return "pending"


def _run(source: dict, run_id: str) -> dict:
    try:
        value = load_run(source, run_id)
        error = None if value else "checkpoint unavailable"
    except (OSError, ValueError, TypeError) as exc:
        value, error = None, display(str(exc))
    return {"run_id": run_id, "checkpoint": value, "diagnostic": error}


def _public_run(row: dict) -> dict:
    value = row["checkpoint"]
    return {"run_id": row["run_id"], "diagnostic": row["diagnostic"],
            "checkpoint": {"status": value["status"], "sequence": value["seq"],
                "last_observed_at": value["ts"], "liveness": "not_verified",
                "flow": plan_flow(value)} if value else None}


def _history(row: dict) -> dict:
    return {"version": row["version"], "kind": display(row.get("kind"), 64),
            "at": display(row.get("at"), 64), "by": display(row.get("by"), 128),
            "reason": display(row.get("reason"), 1024)}


def attach_goals(tasks: list[dict], goals: list[dict]) -> None:
    for task in tasks:
        run_id = task["run_id"] if task["kind"] == "pipeline" else task["lineage"]["parent_run_id"]
        source_id = task["source_id"] if task["kind"] == "pipeline" else task["lineage"]["parent_source_id"]
        task["goal_links"] = [{"goal_id": goal["goal_id"], "statement": goal["statement"],
                                "acceptance": goal["acceptance"]} for goal in goals
                              if goal["source_id"] == source_id
                              and run_id in {row["run_id"] for row in goal["runs"]}]
