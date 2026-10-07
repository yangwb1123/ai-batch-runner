"""Explicit immutable resource registration and protection-only policy updates.

Only private metadata is written. No resources, archives, Git registrations,
leases, execution limits or historical evidence are altered or reclaimed.
"""
from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from .task_center_io import checked_path, _open_regular
from .task_goal_store import resolve_source
from .task_lifecycle import load_run, stamp
from .task_retention_schema import (RESOURCE_PATTERN, MAX_RESOURCES, MAX_BYTES, MAX_AUDIT,
    REVIEW_SECONDS, KINDS, decode, digest, encoded, instant, resource_key, validate_state)
from .task_goal_schema import text
from .task_state_io import atomic_write, locked
from .user_dirs import pbatch_runtime_home


def resource_root() -> Path:
    return pbatch_runtime_home() / "ops" / "retention"


def resource_path(key: str) -> Path:
    if not isinstance(key, str) or not RESOURCE_PATTERN.fullmatch(key):
        raise ValueError("invalid retention resource identity")
    return resource_root() / (key + ".json")


def observed_stat(path: Path) -> dict:
    info = checked_path(path).stat()
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError("retention resources must be regular files or directories")
    return {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid, "mode": info.st_mode,
            "bytes": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def root_identity(path: Path) -> dict:
    return {key: value for key, value in observed_stat(path).items() if key in {"device", "inode", "uid", "mode"}}


def read_record(path: Path) -> dict | None:
    try:
        with _open_regular(path) as handle:
            before = os.fstat(handle.fileno())
            raw = handle.read(MAX_BYTES + 1)
            after = os.fstat(handle.fileno())
        current = checked_path(path).stat()
    except FileNotFoundError:
        return None
    keys = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if keys(before) != keys(after) or keys(after) != keys(current):
        raise ValueError("retention metadata changed during bounded read")
    value = decode(raw)
    if path != resource_path(value["resource_id"]):
        raise ValueError("retention record file identity mismatch")
    return value


def load_resource(key: str) -> dict:
    value = read_record(resource_path(key))
    if value is None:
        raise ValueError("retention resource not found")
    return value


def _actor(by: str, reason: str) -> None:
    if not text(by, 128) or not text(reason, 256):
        raise ValueError("explicit retention actor label and reason required")


def _scope(source: dict, file: str, kind: str) -> tuple[Path, dict, dict]:
    if kind not in KINDS or not isinstance(file, str) or not Path(file).is_absolute():
        raise ValueError("retention registration needs explicit kind and absolute resource path")
    root, path = checked_path(source["data_dir"]), checked_path(file)
    if root not in path.parents:
        raise ValueError("retention resource must be a strict descendant of registered runtime; workspace/root admission is forbidden")
    workspace = Path(source["workspace"]) if source["workspace"] else None
    if workspace and (path == workspace or path in workspace.parents or workspace in path.parents):
        raise ValueError("retention resource cannot overlap a registered workspace")
    metadata = checked_path(resource_root())
    if path == metadata or path in metadata.parents or metadata in path.parents:
        raise ValueError("retention cannot register its own metadata or containing directories")
    root_info, info = root_identity(root), observed_stat(path)
    if not hasattr(os, "geteuid") or any(row["uid"] != os.geteuid() for row in (root_info, info)):
        raise ValueError("resource/root OS uid does not match current effective user; ownership is not inferred")
    if not stat.S_ISDIR(root_info["mode"]) or stat.S_ISREG(info["mode"]) != (kind == "file"):
        raise ValueError("declared resource kind conflicts with observed file type")
    return path, root_info, info


def _records() -> list[dict]:
    rows = []
    for index, path in enumerate(checked_path(resource_root()).iterdir()):
        if index >= MAX_RESOURCES * 2 + 16:
            raise ValueError("retention directory entry budget exhausted; no history was discarded")
        if path.suffix == ".json":
            value = read_record(path)
            if value is None:
                raise ValueError("retention registry changed during registration")
            rows.append(value)
            if len(rows) > MAX_RESOURCES:
                raise ValueError("retention registry exceeds capacity; no records were deleted")
    return rows


def _admit(records: list, value: dict) -> dict | None:
    target = Path(value["source_root"]) / value["relative_path"]
    previous = None
    for row in records:
        path = Path(row["source_root"]) / row["relative_path"]
        if row["resource_id"] == value["resource_id"]:
            previous = row
        elif path == target or path in target.parents or target in path.parents:
            raise ValueError("retention resources overlap; ownership scope was not widened")
        elif (row["registration_stat"]["device"], row["registration_stat"]["inode"]) == (
                value["registration_stat"]["device"], value["registration_stat"]["inode"]):
            raise ValueError("resource already registered through another filesystem identity/path")
    if previous is None and len(records) >= MAX_RESOURCES:
        raise ValueError("retention resource capacity reached; no records were deleted")
    return previous


def register(source_id: str, file: str, kind: str, by: str, reason: str, *,
             confirm: bool = False, run_id: str = "") -> dict:
    if confirm is not True:
        raise ValueError("retention registration needs explicit --confirm; no source/resource was read")
    _actor(by, reason)
    source = resolve_source(source_id)
    path, root_info, info = _scope(source, file, kind)
    relative, at = path.relative_to(Path(source["data_dir"])).as_posix(), stamp()
    review = datetime.fromtimestamp(instant(at) + REVIEW_SECONDS, timezone.utc).isoformat()
    value = {"schema_version": 1, "resource_id": resource_key(source["source_id"], relative),
        "source_id": source["source_id"], "source_root": source["data_dir"], "source_digest": digest(source),
        "relative_path": relative, "kind": kind, "run_id": run_id, "registered_at": at,
        "review_at": review, "version": 1, "pinned": False, "registration_stat": info,
        "root_identity": root_info, "audit": [{"version": 1, "kind": "registered", "by": by,
        "reason": reason, "at": at, "review_at": review}]}
    validate_state(value)
    with locked(resource_root() / ".catalog"):
        if resolve_source(source["source_id"]) != source or _scope(source, file, kind) != (path, root_info, info):
            raise ValueError("source or resource changed during retention registration")
        if run_id and load_run(source, run_id) is None:
            raise ValueError("retention run association needs an actual matching checkpoint; link was not invented")
        previous = _admit(_records(), value)
        if previous is not None:
            return _unchanged(previous, value)
        atomic_write(resource_path(value["resource_id"]), value)
    return value


def _unchanged(previous: dict, value: dict) -> dict:
    fields = ("source_digest", "relative_path", "kind", "run_id", "registration_stat", "root_identity")
    first, initial = previous["audit"][0], value["audit"][0]
    if any(previous[key] != value[key] for key in fields) or (first["by"], first["reason"]) != (initial["by"], initial["reason"]):
        raise ValueError("retention registration cannot be rewritten or renewed; extend/pin metadata explicitly")
    return previous


def protect(key: str, by: str, reason: str, expected: int, until: str = "") -> dict:
    _actor(by, reason)
    if type(expected) is not int or expected < 1:
        raise ValueError("explicit positive retention revision required")
    load_resource(key)  # absent/unsafe records refuse without creating lock files
    with locked(resource_root() / ".catalog"):
        value = load_resource(key)
        if value["version"] != expected:
            raise ValueError("retention revision conflict")
        if value["pinned"]:
            if until:
                raise ValueError("permanent pin needs no extension and cannot be removed")
            return value
        at = stamp()
        if until:
            if instant(until) <= max(instant(value["review_at"]), instant(at)) or len(value["audit"]) >= MAX_AUDIT - 1:
                raise ValueError("extension must increase review date and reserve the final protection slot")
            value["review_at"] = until
        else:
            value["pinned"] = True
        value["version"] += 1
        value["audit"].append({"version": value["version"], "kind": "extended" if until else "pinned",
            "by": by, "reason": reason, "at": at, "review_at": value["review_at"]})
        validate_state(value)
        encoded(value)
        atomic_write(resource_path(key), value)
    return value
