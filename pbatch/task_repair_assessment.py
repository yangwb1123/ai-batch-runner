"""Request-specific inspection of admitted facts, not live recovery preflight.

Metadata matches never clear the seven execution checks. No I/O, dispatch,
process/Git probing, provider calls, budget arithmetic or authority is provided.
"""
from __future__ import annotations

from .task_center_adapters import identity
from .task_lifecycle import validate_run
from .task_repair_preview import REQUIRED_CHECKS, repair_preview
from .task_repair_schema import deadline, fingerprint

VERSION = "repair-request-inspection-v1"


def _check(key: str, matched: bool | None, basis: str, blocker: str) -> dict:
    return {"id": key, "state": "unavailable" if matched is None else
            "observed_match" if matched else "observed_conflict",
            "basis": basis, "blocker": None if matched else blocker}


def _goal_checks(manifest: dict, goal: dict | None) -> list:
    revision = (goal["source_id"] == manifest["source_id"] and goal["goal_id"] == manifest["goal_id"]
        and goal["version"] == manifest["goal_version"] and goal["contract_digest"] == manifest["contract_digest"]
        ) if goal else None
    binding = any(run["run_id"] == manifest["run_id"] for run in goal["runs"]) if revision else None
    return [_check("current_goal_revision", revision, "current_goal_metadata_not_original_execution_contract",
                   "goal_revision_unavailable_or_conflicting"),
            _check("retained_goal_run_binding", binding, "retained_binding_not_version_bound_launch_authorization",
                   "goal_run_binding_unavailable")]


def _checkpoint_checks(manifest: dict, goal: dict | None, observed: tuple | None) -> tuple[list, dict | None]:
    checks, checkpoint = [], observed[0] if observed else None
    try:
        validate_run(checkpoint)
        valid = True
    except (ValueError, TypeError, KeyError):
        valid = False
    checks.append(_check("admitted_checkpoint_schema", valid if observed else None,
                         "admitted_feed_fields_not_fresh_file_bytes", "checkpoint_unavailable_or_invalid"))
    if not valid:
        return checks, None
    matching = (checkpoint["source_id"] == manifest["source_id"] and checkpoint["run_id"] == manifest["run_id"]
        and manifest["task_id"] == "tc-" + identity([checkpoint["source_id"], "pipeline", checkpoint["run_id"]]))
    checks.append(_check("checkpoint_identity", matching, "original_source_run_and_task_fields_not_display_labels",
                         "checkpoint_identity_conflict"))
    pin = {"sequence": checkpoint["seq"], "sha256": fingerprint(checkpoint)}
    checks.append(_check("checkpoint_pin", pin == manifest["checkpoint"],
                         "canonical_admitted_fields_not_file_reverification", "checkpoint_pin_changed"))
    partial = observed[1]
    checks.append(_check("observation_window", not partial, "bounded_feed_admission_not_lifetime_completeness",
                         "partial_observation_window"))
    preview = _preview(manifest, goal, checkpoint, partial)
    stage, reference = preview["candidate_stage"], preview["checkpoint_ref"]
    stage_matches = (matching and pin == manifest["checkpoint"] and stage is not None
                     and stage["stage_key"] == manifest["stage_key"])
    checks.append(_check("same_failed_declared_stage", stage_matches,
                         "complete_sequential_failed_stage_inspection_not_restart_instruction",
                         "same_failed_stage_unavailable"))
    if not stage_matches or reference is None:
        return checks, None
    return checks, preview


def _preview(manifest: dict, goal: dict | None, checkpoint: dict, partial: bool) -> dict:
    task = {"task_id": manifest["task_id"], "source_id": manifest["source_id"], "run_id": manifest["run_id"],
            "kind": "pipeline", "status": checkpoint["status"], "phase": "pipeline",
            "goal_links": [{"goal_id": goal["goal_id"]}] if goal else [], "observations": [],
            "analysis": {"partial_window": partial, "latest_reason": ""}}
    return repair_preview(task, [goal] if goal else [], checkpoint)


def _header_check(manifest: dict, goal: dict | None, observed: tuple | None) -> dict:
    copies = [run["checkpoint"] for run in goal["runs"] if run["run_id"] == manifest["run_id"]] if goal else []
    checkpoint = observed[0] if observed else None
    available = copies and all(copy is not None for copy in copies) and checkpoint
    matched = all((copy["status"], copy["sequence"], copy["last_observed_at"]) ==
                  (checkpoint["status"], checkpoint["seq"], checkpoint["ts"]) for copy in copies) if available else None
    return _check("existing_goal_checkpoint_header", matched,
                  "header_comparison_only_not_content_freshness_process_or_lease_identity",
                  "goal_checkpoint_header_unavailable_or_conflicting")


def _lineage_checks(lineage: dict) -> list:
    return [_check("child_execution_observations", lineage["active_execution_observations"] == 0,
                   "no_active_row_is_not_proof_of_idle_or_dead_processes", "active_child_execution_observations"),
            _check("child_lineage_coverage", not lineage["lineage_diagnostics"] and
                   lineage["unresolved_lineage_observations_in_scoped_sources"] == 0,
                   "explicit_admitted_ancestry_only_unattributed_rows_are_not_assigned_to_this_request",
                   "ambiguous_or_unattributed_child_lineage"),
            _check("child_observation_window", lineage["coverage"] != "partial_window",
                   "bounded_scoped_feeds_not_live_presence_or_lifetime_completeness", "partial_child_observation_window")]


def assess_request(value: dict, goal: dict | None, observed: tuple | None, lineage: dict, now: float) -> dict:
    """Inputs are validated request state and already admitted projections only."""
    manifest = value["manifest"]
    registered = value["state"] == "registered"
    future = deadline(manifest["limits"]["requested_deadline_at"]) > now
    checks = [_check("registered_request", registered, "local_request_record_not_execution_consent", "request_withdrawn"),
              _check("declared_deadline", future, "operator_declaration_not_original_deadline_or_remaining_budget",
                     "declared_deadline_expired")]
    checks.extend(_goal_checks(manifest, goal))
    checkpoint_checks, preview = _checkpoint_checks(manifest, goal, observed)
    checks.extend(checkpoint_checks)
    # Header comparison only uses checkpoints that passed admission validation above.
    admitted = observed if checkpoint_checks[0]["state"] == "observed_match" else None
    checks.append(_header_check(manifest, goal, admitted))
    receipt_matches = (preview["checkpoint_ref"]["plan_sha256"] == value["receipt"]["plan_sha256"]
                       and preview["candidate_stage"]["declared_gate"] == value["receipt"]["declared_gate"]) if preview else None
    checks.append(_check("registered_binding_receipt", receipt_matches,
                         "recorded_plan_digest_and_gate_fields_not_artifact_or_semantic_acceptance", "binding_receipt_unavailable_or_conflicting"))
    checks.extend(_lineage_checks(lineage))
    blockers = list(dict.fromkeys([row["blocker"] for row in checks if row["blocker"]] + list(REQUIRED_CHECKS)))
    candidate = preview if preview and all(row["state"] == "observed_match" for row in checks) else None
    return {"schema_version": 1, "method": VERSION, "decision": "blocked" if registered and future else "inactive",
            "ready_to_execute": False, "grants_execution": False, "live_preflight_performed": False,
            "evidence_basis": "already_admitted_bounded_facts_not_live_reverification",
            "metadata_checks": checks,
            "required_execution_checks": [{"id": key, "state": "required_not_evaluated"} for key in REQUIRED_CHECKS],
            "blockers": blockers, "candidate_stage": candidate["candidate_stage"] if candidate else None,
            "remaining_declared_gates": candidate["remaining_declared_gates"] if candidate else None,
            "child_observations": lineage,
            "original_deadline": None, "remaining_original_budget": None,
            "note": "metadata matches cannot authorize recovery, reuse prior success or reset original limits"}
