"""Immutable, private repair requests; withdrawal is permanent and grants no control."""
from __future__ import annotations

import copy
from contextlib import ExitStack
from pathlib import Path

from .task_center_io import checked_path
from .task_goal_store import _actor, goal_path, resolve_source
from .task_lifecycle import run_path, stamp
from .task_repair_binding import check_binding, read_json
from .task_repair_schema import (MAX_REQUESTS, REQUEST_PATTERN, encoded, fingerprint, matches,
                                 request_key, validate_manifest, validate_state)
from .task_sources import catalog_path
from .task_state_io import atomic_write, locked
from .user_dirs import pbatch_runtime_home


def request_root() -> Path:
    return pbatch_runtime_home() / "ops" / "repair-requests"


def request_path(key: str) -> Path:
    if not matches(REQUEST_PATTERN, key):
        raise ValueError("invalid repair request identity")
    return request_root() / (key + ".json")


def load_request(key: str) -> dict:
    value = read_json(request_path(key))
    if value is None:
        raise ValueError("repair request not found")
    validate_state(value)
    if value["repair_id"] != key:
        raise ValueError("repair file identity mismatch")
    return value


def _capacity(root: Path) -> None:
    count = 0
    for index, path in enumerate(root.iterdir()):
        if index >= MAX_REQUESTS * 2 + 16:
            raise ValueError("repair directory entry budget exhausted; no files were removed")
        if path.suffix == ".json":
            count += 1
    if count >= MAX_REQUESTS:
        raise ValueError("repair request capacity reached; no history was discarded")


def _previous(path: Path, manifest: dict, by: str, reason: str) -> dict | None:
    value = read_json(path)
    if value is None:
        return None
    validate_state(value)
    if value["repair_id"] != request_key(manifest):
        raise ValueError("repair file identity mismatch")
    if value["state"] == "withdrawn":
        raise ValueError("withdrawn repair request cannot be reactivated")
    actor = value["audit"][0]
    if value["manifest"] != manifest or (actor["by"], actor["reason"]) != (by, reason):
        raise ValueError("repair anchor already registered; limits, scope and audit cannot be rewritten")
    return value


def register(source_id: str, manifest: dict, by: str, reason: str) -> dict:
    _actor(by, reason)
    validate_manifest(manifest)
    encoded(manifest)
    manifest = copy.deepcopy(manifest)
    source = resolve_source(source_id)
    check_binding(source, manifest)  # invalid pins/consent inputs do not create locks or state
    key = request_key(manifest)
    with ExitStack() as stack:
        # Same catalog lock used by registration; all acquisitions are nonblocking.
        stack.enter_context(locked(catalog_path().with_suffix("")))
        stack.enter_context(locked(goal_path(manifest["goal_id"])))
        stack.enter_context(locked(run_path(source, manifest["run_id"])))
        stack.enter_context(locked(request_root() / ".catalog"))
        if not checked_path(request_path(key)).exists():
            _capacity(checked_path(request_root()))
        path = stack.enter_context(locked(request_path(key)))
        if resolve_source(source["source_id"]) != source:
            raise ValueError("repair source registration changed")
        receipt = check_binding(source, manifest)
        previous = _previous(path, manifest, by, reason)
        if previous is not None:
            return previous
        value = {"schema_version": 1, "repair_id": key, "manifest": manifest,
            "manifest_digest": fingerprint(manifest), "version": 1, "state": "registered",
            "receipt": receipt, "audit": [{"version": 1, "action": "registered", "by": by,
                "reason": reason, "at": stamp()}]}
        validate_state(value)
        atomic_write(path, value)
    return value


def withdraw(key: str, by: str, reason: str, expected: int) -> dict:
    _actor(by, reason)
    if type(expected) is not int or expected not in (1, 2):
        raise ValueError("explicit repair version must be 1 or 2")
    initial = load_request(key)
    if initial["version"] != expected:
        raise ValueError("repair revision conflict")
    # Does not need the source, goal or checkpoint; stale/expired requests can be withdrawn.
    with locked(request_path(key)) as path:
        value = load_request(key)
        if value["version"] != expected:
            raise ValueError("repair revision conflict")
        if value["state"] == "withdrawn":
            return value
        value["state"], value["version"] = "withdrawn", 2
        value["audit"].append({"version": 2, "action": "withdrawn", "by": by, "reason": reason, "at": stamp()})
        validate_state(value)
        atomic_write(path, value)
    return value
