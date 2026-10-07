"""Metadata-only pin checks. Never process/Git/budget preflight or dispatch."""
from __future__ import annotations

import os
import time
from pathlib import Path

from .task_center_io import _open_regular, checked_path
from .task_goal_store import load_goal
from .task_history_stream import authorized_path
from .task_lifecycle import load_run, stamp
from .task_repair_schema import MAX_BYTES, deadline, decode, fingerprint, validate_manifest


def read_json(path: Path) -> dict | None:
    try:
        handle = _open_regular(path)
    except FileNotFoundError:
        return None
    with handle:
        before = os.fstat(handle.fileno())
        raw = handle.read(MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
        if _stat(before) != _stat(after) or _stat(after) != _stat(checked_path(path).stat()):
            raise ValueError("repair JSON changed during bounded read")
    return decode(raw)


def _stat(value) -> tuple:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def read_manifest(source: dict, file: str) -> dict:
    value = read_json(authorized_path(source, file))
    if value is None:
        raise ValueError("repair manifest file not found")
    return validate_manifest(value)


def check_goal(source: dict, manifest: dict) -> dict:
    goal = load_goal(manifest["goal_id"])
    if goal["source_id"] != source["source_id"] or manifest["source_id"] != source["source_id"]:
        raise ValueError("repair source/goal scope mismatch")
    if (goal["version"] != manifest["goal_version"]
            or goal["contract_digest"] != manifest["contract_digest"]):
        raise ValueError("repair goal revision conflict")
    if {"run_id": manifest["run_id"]} not in goal["bindings"]:
        raise ValueError("repair needs an existing explicit goal/run metadata binding")
    return goal


def check_binding(source: dict, manifest: dict) -> dict:
    check_goal(source, manifest)
    if deadline(manifest["limits"]["requested_deadline_at"]) <= time.time():
        raise ValueError("declared repair request deadline already expired")
    checkpoint = load_run(source, manifest["run_id"])
    if checkpoint is None:
        raise ValueError("repair checkpoint unavailable; no failure was reconstructed")
    pin = manifest["checkpoint"]
    if pin["sequence"] != checkpoint["seq"] or pin["sha256"] != fingerprint(checkpoint):
        raise ValueError("repair checkpoint changed; request pin is stale")
    from .task_center import detail
    task = detail(manifest["task_id"], source["source_id"])
    if task is None or task["kind"] != "pipeline":
        raise ValueError("repair failure is not in the bounded admitted pipeline view")
    preview = task["repair_preview"]
    stage = preview["candidate_stage"]
    reference = preview["checkpoint_ref"]
    if (stage is None or reference is None or stage["stage_key"] != manifest["stage_key"]
            or reference["sha256"] != pin["sha256"]):
        raise ValueError("repair needs one unchanged, complete failed-stage inspection candidate")
    check_goal(source, manifest)
    if deadline(manifest["limits"]["requested_deadline_at"]) <= time.time():
        raise ValueError("declared repair deadline expired during pin checks")
    return {"checked_at": stamp(), "stage_label": stage["label"], "declared_gate": stage["declared_gate"],
            "plan_sha256": reference["plan_sha256"]}
