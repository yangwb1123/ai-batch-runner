"""Shared preservation plans from explicit records; expiration never selects deletion."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from .task_center_io import checked_path, display
from .task_retention_schema import MAX_RESOURCES, REVIEW_SECONDS, digest, instant
from .task_retention_store import read_record, resource_root, observed_stat, root_identity

REQUIRED_CHECKS = (
    "authenticated_resource_disposal_authorization", "exclusive_resource_ownership_and_live_leases",
    "complete_current_references_and_resumability", "git_integration_and_unsaved_work_preserved",
    "independently_verified_recoverable_archive", "atomic_fresh_resource_and_reference_reverification",
    "approved_retention_and_deletion_transaction",
)


def _availability(value: dict, source: dict) -> dict:
    result = {"state": "unavailable", "observed_file_bytes": None, "content_sha256": None,
              "tree_contents_checked": False, "basis": "stat_only_not_bytes_archive_or_exclusive_ownership"}
    if digest({key: source[key] for key in ("source_id", "name", "data_dir", "workspace", "campaign_state", "events_file")}) != value["source_digest"]:
        return {**result, "state": "source_definition_changed"}
    try:
        root = checked_path(source["data_dir"])
        if root_identity(root) != value["root_identity"]:
            return {**result, "state": "source_root_identity_changed"}
        info = observed_stat(root / value["relative_path"])
        if not hasattr(os, "geteuid") or info["uid"] != os.geteuid():
            return {**result, "state": "OS_uid_unavailable_or_conflicting"}
        matching = info == value["registration_stat"]
        file = stat.S_ISREG(info["mode"])
        return {**result, "state": "matching_stat_only" if matching else "stat_changed_preserve",
                "observed_file_bytes": info["bytes"] if file else None}
    except FileNotFoundError:
        return {**result, "state": "missing_not_authorized_deletion"}
    except (ValueError, OSError) as exc:
        return {**result, "diagnostic": display(str(exc), 512)}


def _references(value: dict, goals: list, repairs: list, pipelines: dict, index) -> dict:
    key = (value["source_id"], value["run_id"])
    goal_ids = [goal["goal_id"] for goal in goals if goal["source_id"] == key[0]
                and key[1] in {run["run_id"] for run in goal["runs"]}]
    request_ids = [row["repair_id"] for row in repairs if (row["source_id"], row["run_id"]) == key
                   and row["state"] == "registered"]
    observed = pipelines.get(key)
    children = index.inspection({key}) if key[1] else None
    return {"basis": "explicit_bound_run_and_admitted_windows_not_complete_reference_graph",
        "run_association": "registration_metadata_not_ownership_or_execution_authorization" if key[1] else "untracked",
        "bound_run_observed_status": observed[0] if observed else None,
        "bound_run_window_partial": observed[1] if observed else None,
        "goal_refs": goal_ids[:8], "observed_goal_references": len(goal_ids), "omitted_goal_refs": max(0, len(goal_ids) - 8),
        "repair_refs": request_ids[:8], "observed_registered_repair_references": len(request_ids),
        "omitted_repair_refs": max(0, len(request_ids) - 8), "children": children,
        "liveness": "not_verified", "unobserved_references": "unknown_not_zero"}


def _public(value: dict, source: dict, goals: list, repairs: list, pipelines: dict, index, now: float) -> dict:
    availability = _availability(value, source)
    refs = _references(value, goals, repairs, pipelines, index)
    due = instant(value["review_at"]) <= now
    policy = "permanently_pinned" if value["pinned"] else "review_due_preserve" if due else "retention_window_preserve"
    blockers = list(REQUIRED_CHECKS)
    blockers.append(policy)
    if availability["state"] != "matching_stat_only":
        blockers.append("resource_stat_or_scope_unavailable_or_changed")
    if value["kind"] != "file":
        blockers.append("directory_contents_and_Git_state_not_verified")
    if refs["observed_goal_references"] or refs["observed_registered_repair_references"]:
        blockers.append("retained_goal_or_repair_metadata_references")
    if refs["bound_run_observed_status"] in {"planned", "running"} or (refs["children"] and refs["children"]["active_execution_observations"]):
        blockers.append("active_run_or_child_execution_observations")
    blockers.extend(_reference_blockers(refs))
    return {"schema_version": 1, "method": "registered-resource-preservation-v1",
        "resource_id": value["resource_id"], "source_id": value["source_id"], "project": source["name"],
        "relative_path_display": display(value["relative_path"], 2048), "declared_kind": value["kind"],
        "kind_basis": "operator_declaration_not_Git_worktree_verification", "run_id": value["run_id"] or None,
        "version": value["version"], "pinned": value["pinned"], "registered_at": value["registered_at"],
        "review_at": value["review_at"], "review_due": due, "policy": policy,
        "initial_review_seconds": REVIEW_SECONDS, "policy_basis": "metadata_review_date_not_original_execution_deadline",
        "availability": availability, "references": refs, "plan": {"decision": "preserve", "blockers": blockers,
            "required_disposal_checks": [{"id": check, "state": "required_not_evaluated"} for check in REQUIRED_CHECKS]},
        "archive_verified": False, "estimated_reclaimable_bytes": None,
        "audit": [{**row, "by": display(row["by"], 128), "reason": display(row["reason"], 256)} for row in value["audit"]],
        "actor_identity": "unauthenticated_local_label", "cleanup_authority": "not_granted",
        "capabilities": {"cleanup": False, "archive": False, "merge": False, "resume": False, "feedback_upload": False},
        "note": "age, size, matching stat, old PASS or retained binding cannot authorize removal"}


def _reference_blockers(refs: dict) -> list[str]:
    blockers = []
    if refs["bound_run_observed_status"] is None:
        blockers.append("bound_run_or_reference_graph_untracked_in_window")
    children = refs["children"]
    if refs["bound_run_window_partial"] or (children and children["coverage"] == "partial_window"):
        blockers.append("partial_reference_observation_window")
    if children and (children["unresolved_lineage_observations_in_scoped_sources"] or children["lineage_diagnostics"]):
        blockers.append("incomplete_or_ambiguous_scoped_lineage")
    return blockers


def resource_views(sources: list, goals: list, repairs: list, candidates: list, index, now: float) -> tuple[list, list]:
    selected = {source["source_id"]: source for source in sources}
    pipelines = {(source["source_id"], rows[-1]["run_id"]): (rows[-1]["status"], partial)
                 for _, _, source, rows, partial in candidates if rows[-1]["kind"] == "pipeline"}
    results, notes = [], []
    try:
        root = checked_path(resource_root())
        if not root.is_dir():
            return results, notes
        for number, path in enumerate(root.iterdir()):
            if number >= MAX_RESOURCES * 2 + 16 or len(results) >= MAX_RESOURCES:
                notes.append("retention registry observation window limited; no cleanup clearance")
                break
            if path.suffix != ".json":
                continue
            try:
                value = read_record(path)
                if value and value["source_id"] in selected:
                    results.append(_public(value, selected[value["source_id"]], goals, repairs, pipelines, index, now))
            except (ValueError, OSError, TypeError) as exc:
                notes.append("retention resource invalid: " + display(str(exc), 512))
    except (ValueError, OSError) as exc:
        notes.append("retention registry unavailable: " + display(str(exc), 512))
    return results, notes


def attach_resources(tasks: list, goals: list, resources: list) -> None:
    for task in tasks:
        task["resource_links"] = [row["resource_id"] for row in resources if task["kind"] == "pipeline"
            and (row["source_id"], row["run_id"]) == (task["source_id"], task["run_id"])]
    for goal in goals:
        goal["resource_links"] = [row["resource_id"] for row in resources if row["source_id"] == goal["source_id"]
            and row["run_id"] in {run["run_id"] for run in goal["runs"]}]
