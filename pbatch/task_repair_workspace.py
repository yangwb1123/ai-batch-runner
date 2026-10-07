"""Explicit one-shot workspace diagnostics, never recovery preflight or stored grants."""
from __future__ import annotations

import time
from pathlib import Path

from .task_center_io import display
from .task_goal_store import resolve_source
from .task_lifecycle import stamp
from .task_repair_preview import REQUIRED_CHECKS
from .task_repair_schema import fingerprint
from .task_repair_store import load_request
from .task_workspace_layout import admit_layout, metadata_stamp, operation_markers
from .task_workspace_sandbox import frozen_controls
from . import task_workspace_git as git

TOTAL_TIMEOUT = 15.0
VERSION = "repair-workspace-inspection-v1"


def _admitted(value: dict, source: dict) -> dict:
    from .task_center import snapshot
    rows = snapshot(source_id=source["source_id"])["repairs"]
    record = next((row for row in rows if row["repair_id"] == value["repair_id"]), None)
    if record is None or record["status"] != "declared_request_only" or record["assessment"]["candidate_stage"] is None:
        raise ValueError("request is stale, inactive, incomplete or has child/lineage blockers; no Git inspection performed")
    return record


def _base(value: dict, source: dict, expected_head: str) -> dict:
    return {"schema_version": 1, "method": VERSION, "repair_id": value["repair_id"],
        "source_id": source["source_id"], "task_id": value["manifest"]["task_id"],
        "manifest_digest": value["manifest_digest"], "request_version": value["version"],
        "workspace_display": display(source["workspace"], 1024),
        "workspace_binding": "registered_source_workspace_not_verified_original_run_workspace",
        "expected_head": expected_head, "expectation_basis": "operator_supplied_oid_not_original_run_baseline",
        "original_git_baseline": None, "observed_at": stamp(), "git_sampling_attempted": False, "git_sampling_completed": False,
        "git_observation": None, "directory_uid_basis": "OS_uid_only_not_authenticated_or_exclusive_execution_ownership",
        "inspection_state": "unavailable", "diagnostic": None,
        "diagnostic_basis": "bounded_recorded_text_not_cause_classification_or_instructions",
        "decision": "blocked", "ready_to_execute": False, "grants_execution": False,
        "live_preflight_performed": False, "writes_workspace": False, "stores_receipt": False,
        "private_temporary_controls_created": False,
        "blockers": list(REQUIRED_CHECKS), "total_timeout_seconds": TOTAL_TIMEOUT,
        "note": "one-shot observation; no merge, cleanup, reset, dispatch or immutable-time snapshot claim"}


def _samples(layout: dict, expected_head: str, deadline: float) -> tuple[dict, list]:
    before = git.identity(layout, deadline)
    links = git.gitlinks(layout, deadline)
    raw, status = git.status(layout, deadline)
    after = git.identity(layout, deadline)
    again, second_status = git.status(layout, deadline)
    if before != after or raw != again or status != second_status:
        raise ValueError("Git HEAD, branch or status changed between bounded samples")
    blockers = []
    if before["head"] != expected_head:
        blockers.append("operator_head_expectation_conflict")
    if before["branch"] is None:
        blockers.append("detached_head_requires_original_branch_scope_evidence")
    if links:
        blockers.append("submodule_workspaces_not_inspected")
    if status["counts"]["tracked"] or status["counts"]["untracked"]:
        blockers.append("reported_dirty_workspace_preserve_changes")
    if status["counts"]["ignored"]:
        blockers.append("ignored_entries_present_not_evidence_of_safe_cleanup")
    return {"head": before["head"], "branch_display": before["branch"], "head_matches_expectation": before["head"] == expected_head,
        "status": status, "tracked_gitlinks": links,
        "config_basis": "private_minimal_controls_not_original_repository_config_filter_or_ignore_semantics",
        "sampling_basis": "two_samples_using_private_frozen_controls_not_atomic_content_or_liveness_verification"}, blockers


def _inputs(expected_head: str, common_dir: str, confirm: bool) -> None:
    if confirm is not True:
        raise ValueError("workspace inspection needs explicit --confirm; no request or workspace was read")
    if not isinstance(expected_head, str) or not git.OID.fullmatch(expected_head):
        raise ValueError("explicit --expected-head must be one complete lowercase Git object ID")
    if common_dir and (not isinstance(common_dir, str) or not Path(common_dir).is_absolute()):
        raise ValueError("--common-dir must be an explicitly selected absolute Git metadata directory")


def inspect_workspace(key: str, expected_head: str, *, confirm: bool = False,
                      common_dir: str = "", source_id: str = "") -> dict:
    _inputs(expected_head, common_dir, confirm)  # before request, source or workspace reads
    value = load_request(key)
    source = resolve_source(value["manifest"]["source_id"])
    if source_id and source_id not in {source["source_id"], source["name"]}:
        raise ValueError("workspace inspection source scope mismatch")
    _admitted(value, source)
    report = _base(value, source, expected_head)
    start = time.monotonic()
    try:
        if not source["workspace"]:
            raise ValueError("registered source has no explicit workspace; run workspace was not guessed")
        layout = admit_layout(source["workspace"], common_dir)
        markers = operation_markers(layout)
        if markers:
            report["operation_markers"] = markers
            raise ValueError("Git lock or in-progress operation markers exist; preserve them without recovery commands")
        before = metadata_stamp(layout)
        with frozen_controls(layout) as private_layout:
            report["private_temporary_controls_created"] = True
            report["git_sampling_attempted"] = True
            observation, blockers = _samples(private_layout, expected_head, start + TOTAL_TIMEOUT)
            report["git_sampling_completed"] = True
        if admit_layout(source["workspace"], common_dir) != layout or metadata_stamp(layout) != before:
            raise ValueError("workspace or Git metadata identity/stat window changed during inspection")
        if operation_markers(layout):
            raise ValueError("Git operation markers appeared during inspection")
        if resolve_source(source["source_id"]) != source or fingerprint(load_request(key)) != fingerprint(value):
            raise ValueError("source registration or request changed during workspace inspection")
        _admitted(value, source)
        if time.monotonic() > start + TOTAL_TIMEOUT:
            raise ValueError("shared workspace-inspection deadline exhausted")
        report.update(git_observation=observation, inspection_state="observed", layout=layout["layout"])
        report["blockers"].extend(blockers)
    except (OSError, ValueError) as exc:
        report["diagnostic"] = display(str(exc), 1024)
        report["blockers"].append("workspace_evidence_unavailable_or_changed")
    report["elapsed_seconds"] = round(time.monotonic() - start, 6)
    return report
