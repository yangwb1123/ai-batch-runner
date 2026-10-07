"""Explicit local immutable synthetic-case drafts; no upload, execution or promotion."""
from __future__ import annotations

import os
from pathlib import Path

from .task_center_io import checked_path, display
from .task_goal_schema import text
from .task_lifecycle import stamp
from .task_sources import catalog_path, registered_sources
from .task_state_io import atomic_write, locked
from .user_dirs import pbatch_runtime_home
from .task_feedback_io import private_root, proof, publish, read_bytes, stat_key
from .task_feedback_schema import (FEEDBACK_PATTERN, MAX_DRAFTS, MAX_INPUT_BYTES, MAX_METADATA_BYTES,
    decode, digest, feedback_key, state_bytes, validate_case, validate_state)


def feedback_root() -> Path:
    return pbatch_runtime_home() / "ops" / "feedback"


def feedback_path(key: str) -> Path:
    if not isinstance(key, str) or not FEEDBACK_PATTERN.fullmatch(key):
        raise ValueError("invalid feedback identity")
    return feedback_root() / (key + ".json")


def load_feedback(key: str) -> dict:
    path = feedback_path(key)
    private_root(path.parent)
    raw, _ = read_bytes(path, MAX_METADATA_BYTES, private=True)
    value = validate_state(decode(raw, MAX_METADATA_BYTES))
    if value["feedback_id"] != key:
        raise ValueError("feedback file identity mismatch")
    return value


def _actor(by: str, reason: str) -> None:
    if not text(by, 128) or not text(reason, 256):
        raise ValueError("explicit feedback actor label and reason are required")


def _source(key: str) -> dict:
    matches = [item for item in registered_sources() if key in {item["source_id"], item["name"]}]
    if len(matches) != 1:
        raise ValueError("feedback requires one explicitly registered source; discovery alone grants no case access")
    return matches[0]


def _input_path(source: dict, file: str) -> Path:
    if not isinstance(file, str) or not file:
        raise ValueError("feedback needs one explicitly selected JSON file")
    path = Path(file)
    if not path.is_absolute():
        if not source["workspace"]:
            raise ValueError("relative feedback input needs an explicitly registered workspace")
        path = Path(source["workspace"]) / path
    path, root = checked_path(path), checked_path(feedback_root())
    scopes = [Path(source["data_dir"])] + ([Path(source["workspace"])] if source["workspace"] else [])
    if not any(scope in path.parents for scope in scopes):
        raise ValueError("feedback input must be inside explicitly registered roots")
    _storage_scope(root)
    if path == root or root in path.parents or path in root.parents:
        raise ValueError("feedback cannot import its own metadata or copies")
    if root.exists():
        private_root(root)
    return path


def _storage_scope(root: Path) -> None:
    for item in registered_sources():
        for field in ("data_dir", "workspace"):
            scope = Path(item[field]) if item[field] else None
            if scope and (scope == root or scope in root.parents or root in scope.parents):
                raise ValueError("private feedback storage must not overlap registered runtime/workspace roots")


def _records(root: Path) -> list[dict]:
    rows = []
    for number, path in enumerate(root.iterdir()):
        if number >= MAX_DRAFTS * 2 + 16:
            raise ValueError("feedback directory entry budget exhausted; no evidence was removed")
        if path.suffix == ".json":
            rows.append(load_feedback(path.stem))
            if len(rows) > MAX_DRAFTS:
                raise ValueError("feedback registry exceeds capacity; no history was discarded")
    return rows


def _new(source: dict, case: dict, reference: dict, by: str, reason: str) -> dict:
    at = stamp()
    value = {"schema_version": 1, "feedback_id": feedback_key(source["source_id"], case["case_id"]),
        "source_id": source["source_id"], "source_digest": digest(source), "case_id": case["case_id"],
        "case_sha256": digest(case), "input": reference, "summary": display(case["summary"], 256),
        "created_at": at, "version": 1, "state": "draft",
        "audit": [{"version": 1, "action": "drafted", "by": by, "reason": reason, "at": at}]}
    return validate_state(value)


def _recheck(source: dict, path: Path, observed: tuple) -> None:
    if _source(source["source_id"]) != source or _input_path(source, str(path)) != path or stat_key(path.stat()) != observed:
        raise ValueError("feedback source/input changed during registration; no metadata published")


def _verify_case(value: dict) -> None:
    raw, _ = read_bytes(feedback_root() / (value["feedback_id"] + ".case"), MAX_INPUT_BYTES, private=True)
    if (proof(Path(value["input"]["path"]), raw) != value["input"] or
            digest(validate_case(decode(raw, MAX_INPUT_BYTES))) != value["case_sha256"]):
        raise ValueError("feedback private copy changed; no existing evidence was overwritten")


def _unchanged(previous: dict, value: dict) -> dict:
    if previous["state"] == "withdrawn":
        raise ValueError("feedback withdrawal is permanent; draft cannot be reactivated")
    keys = ("source_id", "source_digest", "case_id", "case_sha256", "input", "summary")
    first, audit = previous["audit"][0], value["audit"][0]
    if any(previous[key] != value[key] for key in keys) or (first["by"], first["reason"]) != (audit["by"], audit["reason"]):
        raise ValueError("feedback case/source/input/audit cannot be rewritten; use an explicitly different case ID")
    _verify_case(previous)
    return previous


def draft(source_id: str, file: str, by: str, reason: str, *, confirm: bool = False) -> dict:
    if confirm is not True:
        raise ValueError("feedback drafting needs explicit --confirm; no source or case was read")
    _actor(by, reason)
    source = _source(source_id)
    path = _input_path(source, file)
    raw, observed = read_bytes(path, MAX_INPUT_BYTES)
    case = validate_case(decode(raw, MAX_INPUT_BYTES))
    value = _new(source, case, proof(path, raw), by, reason)
    root = checked_path(feedback_root())
    # Source catalog uses task-sources.lock, not task-sources.json.lock.
    with locked(catalog_path().with_suffix("")), locked(root / ".publication"):
        _recheck(source, path, observed)
        private_root(root)
        records = _records(root)
        previous = next((row for row in records if row["feedback_id"] == value["feedback_id"]), None)
        if previous is not None:
            return _unchanged(previous, value)
        if len(records) >= MAX_DRAFTS:
            raise ValueError("feedback capacity reached; no old drafts or orphan evidence were deleted")
        publish(root / (value["feedback_id"] + ".case"), raw)
        _verify_case(value)
        _recheck(source, path, observed)
        publish(feedback_path(value["feedback_id"]), state_bytes(value))
    return value


def withdraw(key: str, by: str, reason: str, expected: int) -> dict:
    _actor(by, reason)
    if type(expected) is not int or expected < 1:
        raise ValueError("explicit positive feedback revision required")
    load_feedback(key)  # missing state refuses without creating a lock or repairing storage
    with locked(feedback_root() / ".publication"):
        value = load_feedback(key)
        if value["version"] != expected:
            raise ValueError("feedback revision conflict")
        if value["state"] == "withdrawn":
            return value
        value["state"], value["version"] = "withdrawn", 2
        value["audit"].append({"version": 2, "action": "withdrawn", "by": by, "reason": reason, "at": stamp()})
        validate_state(value)
        state_bytes(value)
        atomic_write(feedback_path(key), value)
    return value
