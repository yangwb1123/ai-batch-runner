"""Deterministic remaining-step projection; a ready display is not dispatch."""
from __future__ import annotations

from .task_center_io import display


def public_plan(checkpoint: dict) -> dict:
    return {"origin": "runtime_declared_pipeline", "complete": checkpoint["plan_complete"],
            "grants_execution": False,
            "steps": [{"id": display(step["id"], 160), "state": step["state"],
                "dependencies": [display(dep, 160) for dep in step["dependencies"]],
                "gate": step["gate"]} for step in checkpoint["steps"]]}


def plan_flow(checkpoint: dict) -> dict:
    steps = checkpoint["steps"]
    current = checkpoint["stage"] or None
    names = [step["id"] for step in steps]
    previous = names[names.index(current) - 1] if current in names and names.index(current) > 0 else None
    states = {step["id"]: step["state"] for step in steps}
    result = {"last_observed_stage": display(current) if current else None,
              "previous_observed_stage": display(previous) if previous else None,
              "previous_observed_status": states.get(previous),
              "next_stage": None, "next_status": "untracked", "plan_complete": checkpoint["plan_complete"],
              "origin": "runtime_checkpoint", "grants_execution": False,
              "note": "declared sequential stage order and dependencies, not an AI recommendation"}
    if not checkpoint["plan_complete"]:
        return result
    if checkpoint["status"] in {"failed", "cancelled", "interrupted"}:
        result.update(next_status="blocked_by_" + checkpoint["status"])
        return result
    pending = [step for step in steps if step["state"] == "pending"]
    if not pending:
        result["next_status"] = "no_remaining_declared_stage"
        return result
    step = pending[0]
    result["next_stage"] = display(step["id"])
    result["next_status"] = ("planned_ready" if all(states[dep] == "succeeded"
                            for dep in step["dependencies"]) else "planned_waiting")
    return result
