"""Bounded recovery inspection proposals, never live preflight or execution grants.

Only admitted task/checkpoint/goal facts enter this deterministic projection.
No filesystem, process, Git, provider, budget or executor is consulted. A stage
key binds the original identity; redacted display labels are not routing keys.
"""
from __future__ import annotations

import hashlib
import json

from .task_center_adapters import identity
from .task_center_io import display
from .task_lifecycle import validate_run

MAX_REFS = 8
MAX_STEPS = 8
VERSION = "failure-inspection-v1"
_FAILURES = {
    "failed": "reported_failure", "rejected": "reported_rejection",
    "pipeline_failed": "reported_pipeline_failure", "gate_rejected": "reported_gate_rejection",
    "validation_failed": "reported_validation_failure", "interrupted": "reported_interruption",
    "cancelled": "reported_cancellation", "merge_blocked": "reported_merge_block",
    "budget_blocked": "reported_budget_block", "provider_breaker_paused": "reported_provider_pause",
    "recovery_blocked": "reported_recovery_block",
}
REQUIRED_CHECKS = (
    "authenticated_execution_authorization", "original_goal_execution_binding",
    "persisted_original_deadline_and_remaining_budgets", "current_process_and_lease_liveness",
    "workspace_ownership_git_baseline_and_dirty_state", "fresh_checkpoint_and_artifact_evidence",
    "current_engineering_merge_and_independent_acceptance_gates",
)
_HINTS = {
    "reported_cancellation": "respect_cancellation_require_separate_restart_authorization",
    "reported_interruption": "inspect_interruption_without_assuming_executor_stopped",
    "reported_merge_block": "inspect_merge_block_without_merging_or_relaxing_scope",
    "reported_budget_block": "inspect_original_budget_without_resetting_or_increasing_it",
    "reported_provider_pause": "inspect_provider_pause_without_clearing_breaker_or_retrying",
    "reported_recovery_block": "preserve_failed_workspace_inspect_recovery_block",
    "reported_gate_rejection": "inspect_gate_rejection_keep_verdict_gate_closed",
    "reported_validation_failure": "inspect_validation_failure_keep_validator_unchanged",
}


def _digest(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _stage_ref(checkpoint: dict, step: dict) -> dict:
    return {"stage_key": "st-" + identity([checkpoint["source_id"], checkpoint["run_id"], step["id"]]),
            "label": display(step["id"], 160), "recorded_state": step["state"],
            "declared_gate": step["gate"]}


def _scope(task: dict, goals: list) -> dict:
    linked = {link["goal_id"] for link in task["goal_links"]}
    matches = [goal for goal in goals if goal["goal_id"] in linked]
    return {"goal_contracts": [{"goal_id": goal["goal_id"], "source_id": goal["source_id"],
                "version": goal["version"], "contract_digest": goal["contract_digest"],
                "acceptance": goal["acceptance"], "binding_basis": "retained_metadata_not_version_bound_execution"}
                for goal in matches[:MAX_REFS]],
            "goal_contracts_count": len(matches), "omitted_goal_contracts": max(0, len(matches) - MAX_REFS),
            "goal_selection": "untracked" if not matches else "ambiguous_multiple_goals" if len(matches) > 1
                              else "one_linked_metadata_goal_not_execution_scope_verification"}


def _base(task: dict, goals: list) -> dict:
    trigger = _FAILURES.get(task["status"].lower())
    if trigger in {"reported_failure", "reported_rejection"} and task["phase"] == "validation":
        trigger = "reported_validation_failure"
    observations = task["observations"][-MAX_REFS:]
    return {"schema_version": 1, "method": VERSION,
            "applicability": "failure_observation" if trigger else "not_applicable",
            "trigger": trigger, "inspection_hint": _HINTS.get(trigger, "inspect_reported_failure") if trigger else None,
            "evidence_basis": "bounded_admitted_observations_not_live_reverification",
            "task_id": task["task_id"], "source_id": task["source_id"], "run_id": task["run_id"],
            "recorded_status": task["status"], "recorded_phase": task["phase"],
            "recorded_diagnostic": {"text": task["analysis"]["latest_reason"],
                "basis": "recorded_text_not_cause_classification_or_instructions"},
            "observation_refs": observations,
            "omitted_observation_refs": max(0, len(task["observations"]) - len(observations)),
            "scope": _scope(task, goals), "checkpoint_ref": None, "candidate_stage": None,
            "durable_checkpoint_comparison": "not_available_in_existing_goal_projection",
            "remaining_declared_steps": [], "omitted_remaining_steps": 0,
            "remaining_declared_gates": None, "stage_selection": "not_selected",
            "required_checks": [{"id": key, "state": "required_not_evaluated"} for key in REQUIRED_CHECKS] if trigger else [],
            "blockers": [], "authority": {"grants_execution": False, "live_preflight_performed": False,
                "resume": False, "retry": False, "merge": False, "cleanup": False,
                "provider_breaker_reset": False, "budget_or_deadline_reset": False},
            "note": "inspection proposal only; no commands, model calls, state writes or dispatch"}


def _blockers(task: dict, preview: dict) -> list[str]:
    blockers = list(REQUIRED_CHECKS)
    scope = preview["scope"]
    if not scope["goal_contracts_count"]:
        blockers.append("confirmed_goal_metadata_untracked")
    elif scope["goal_contracts_count"] > 1:
        blockers.append("multiple_goal_scopes_need_explicit_selection")
    if task["analysis"]["partial_window"]:
        blockers.append("partial_observation_window")
    consumption = task.get("consumption") or {}
    if consumption.get("active_execution_observations", 0):
        blockers.append("active_child_execution_observations")
    if consumption.get("lineage_diagnostics"):
        blockers.append("incomplete_or_ambiguous_child_lineage")
    if preview["trigger"] in {"reported_cancellation", "reported_budget_block",
                               "reported_provider_pause", "reported_recovery_block", "reported_merge_block"}:
        blockers.append(preview["trigger"])
    return blockers


def _checkpoint(preview: dict, task: dict, value: dict | None) -> dict | None:
    if value is None:
        preview["blockers"].append("declared_pipeline_checkpoint_untracked")
        return None
    try:
        validate_run(value)
        if value["source_id"] != task["source_id"] or value["run_id"] != task["run_id"]:
            raise ValueError("checkpoint identity mismatch")
    except (ValueError, TypeError, KeyError):
        preview["blockers"].append("invalid_or_mismatched_checkpoint")
        return None
    preview["checkpoint_ref"] = {"run_id": value["run_id"], "source_id": value["source_id"],
        "sequence": value["seq"], "last_observed_at": value["ts"], "sha256": _digest(value),
        "plan_sha256": _digest({"steps": value["steps"], "complete": value["plan_complete"]}),
        "sha256_basis": "canonical_checkpoint_fields_not_file_bytes",
        "basis": "lifecycle_feed_snapshot_not_fresh_durable_checkpoint"}
    if value["status"] != task["status"]:
        preview["blockers"].append("checkpoint_status_conflict")
        return None
    if not value["plan_complete"]:
        preview["blockers"].append("declared_plan_incomplete")
        return None
    return value


def _checkpoint_conflict(preview: dict, task: dict, value: dict, goals: list) -> bool:
    linked = {link["goal_id"] for link in task["goal_links"]}
    copies = [run["checkpoint"] for goal in goals
              if goal["goal_id"] in linked and goal["source_id"] == value["source_id"]
              for run in goal["runs"] if run["run_id"] == value["run_id"]]
    known = [copy for copy in copies if copy is not None]
    if any((copy["status"], copy["sequence"], copy["last_observed_at"]) !=
           (value["status"], value["seq"], value["ts"]) for copy in known):
        preview["durable_checkpoint_comparison"] = "header_conflict_with_existing_goal_projection"
        preview["blockers"].append("durable_checkpoint_header_conflicts_with_feed")
        return True
    if known:
        preview["durable_checkpoint_comparison"] = "header_matching_only_not_content_or_liveness"
    return False


def _failed_index(preview: dict, value: dict) -> int | None:
    if value["status"] != "failed":
        preview["stage_selection"] = "no_restart_candidate_for_cancellation_or_interruption"
        return None
    steps = value["steps"]
    selected = [index for index, step in enumerate(steps)
                if step["id"] == value["stage"] and step["state"] == "failed"]
    if len(selected) != 1:
        preview["blockers"].append("failed_declared_stage_not_unambiguous")
        return None
    index = selected[0]
    if (any(step["state"] != "succeeded" for step in steps[:index])
            or any(step["state"] != "pending" for step in steps[index + 1:])):
        preview["blockers"].append("ambiguous_sequential_checkpoint_outcomes")
        return None
    return index


def _candidate(preview: dict, task: dict, value: dict | None, goals: list) -> None:
    value = _checkpoint(preview, task, value)
    if value is None or _checkpoint_conflict(preview, task, value, goals):
        return
    index = _failed_index(preview, value)
    if index is None:
        return
    steps = value["steps"]
    if task["analysis"]["partial_window"]:
        preview["stage_selection"] = "withheld_partial_observation_window"
        return
    step = steps[index]
    prerequisites = [item for item in steps if item["id"] in step["dependencies"]]
    preview["candidate_stage"] = {**_stage_ref(value, step),
        "kind": "inspect_same_failed_declared_stage_not_restart_instruction",
        "prerequisites": [_stage_ref(value, item) for item in prerequisites[:MAX_REFS]],
        "omitted_prerequisites": max(0, len(prerequisites) - MAX_REFS),
        "prerequisite_reuse": "not_approved_even_if_previously_succeeded"}
    remaining = steps[index:]
    preview["remaining_declared_steps"] = [_stage_ref(value, item) for item in remaining[:MAX_STEPS]]
    preview["omitted_remaining_steps"] = max(0, len(remaining) - MAX_STEPS)
    preview["remaining_declared_gates"] = sum(item["gate"] for item in remaining)
    preview["stage_selection"] = "observed_failed_stage_inspection_only"


def repair_preview(task: dict, goals: list, checkpoint: dict | None = None) -> dict:
    preview = _base(task, goals)
    if preview["trigger"] is None:
        return preview
    preview["blockers"] = _blockers(task, preview)
    if task["kind"] == "pipeline":
        _candidate(preview, task, checkpoint, goals)
    else:
        preview["blockers"].append("no_declared_pipeline_plan_for_this_record")
    return preview


def attach_repair_previews(tasks: list, goals: list, candidates: list) -> None:
    """Use already admitted raw checkpoints; never reconstruct from UI labels."""
    checkpoints = {"tc-" + key: rows[-1].get("checkpoint")
                   for _, key, _, rows, _ in candidates if rows[-1]["kind"] == "pipeline"}
    for task in tasks:
        task["repair_preview"] = repair_preview(task, goals, checkpoints.get(task["task_id"]))
