"""Explicit single-file preservation and byte verification; no complete-archive grants."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .task_center_io import checked_path, display
from .task_goal_store import resolve_source
from .task_lifecycle import stamp
from .task_retention_store import load_resource, resource_root, _scope, _actor
from .task_retention_schema import digest
from .task_state_io import locked
from .task_preservation_schema import (PRESERVATION_PATTERN, MAX_RECEIPTS, MAX_PAYLOAD_BYTES,
    receipt_key, validate_receipt)
from .task_preservation_io import copy_stream, publish, read_receipt, remove_owned_temporary
from .user_dirs import pbatch_runtime_home


def preservation_root() -> Path:
    return pbatch_runtime_home() / "preserved" / "resources"


def receipt_path(key: str) -> Path:
    if not isinstance(key, str) or not PRESERVATION_PATTERN.fullmatch(key):
        raise ValueError("invalid preservation identity")
    return preservation_root() / (key + ".json")


def load_receipt(key: str) -> dict:
    value = read_receipt(receipt_path(key))
    if value is None:
        raise ValueError("preservation receipt not found")
    if value["preservation_id"] != key:
        raise ValueError("preservation receipt file identity mismatch")
    return value


def _source(value: dict, expected: int) -> tuple:
    if type(expected) is not int or expected != value["version"]:
        raise ValueError("explicit preservation resource revision conflicts")
    if value["kind"] != "file":
        raise ValueError("only registered regular files may be copied; directories/worktrees need separate complete recovery support")
    source = resolve_source(value["source_id"])
    if digest(source) != value["source_digest"]:
        raise ValueError("preservation source definition changed")
    path, root, info = _scope(source, str(Path(value["source_root"]) / value["relative_path"]), "file")
    identity = ("device", "inode", "uid", "mode")
    if root != value["root_identity"] or any(info[key] != value["registration_stat"][key] for key in identity):
        raise ValueError("preservation path/root identity changed; a new object was not adopted")
    if info["bytes"] > MAX_PAYLOAD_BYTES:
        raise ValueError("preservation source exceeds payload byte budget; no truncation")
    archive = checked_path(preservation_root())
    runtime, workspace = Path(source["data_dir"]), Path(source["workspace"]) if source["workspace"] else None
    if (archive == runtime or runtime in archive.parents or path == archive or path in archive.parents or
            archive in path.parents or (workspace and (archive == workspace or workspace in archive.parents))):
        raise ValueError("preservation storage must be independent of registered runtime/workspace and copied objects")
    return source, path, info


def _capacity(root: Path) -> None:
    receipts = 0
    for number, path in enumerate(root.iterdir()):
        if number >= MAX_RECEIPTS * 2 + 16:
            raise ValueError("preservation directory entry budget exhausted; no files were removed")
        if path.suffix == ".json":
            load_receipt(path.stem)
            receipts += 1
    if receipts >= MAX_RECEIPTS:
        raise ValueError("preservation capacity reached; no old recovery evidence was deleted")


def preserve(key: str, by: str, reason: str, expected: int, *, confirm: bool = False) -> dict:
    if confirm is not True:
        raise ValueError("preservation needs explicit --confirm; no source bytes were read")
    _actor(by, reason)
    initial = load_resource(key)
    _source(initial, expected)
    root = checked_path(preservation_root())
    with locked(resource_root() / ".catalog"), locked(root / ".publication"):
        value = load_resource(key)
        source, path, info = _source(value, expected)
        _private_root(root)
        descriptor, name = tempfile.mkstemp(prefix=".preservation-", dir=root)
        temporary = Path(name)
        created = os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, "wb") as output:
                payload = copy_stream(path, output, info)
                output.flush()
                os.fsync(output.fileno())
            if resolve_source(value["source_id"]) != source or _source(load_resource(key), expected) != (source, path, info):
                raise ValueError("source/resource changed while preserving; no receipt published")
            receipt = {"schema_version": 1, "resource_id": key, "resource_version": expected,
                "source_id": value["source_id"], "source_digest": value["source_digest"], "source_stat": info,
                "payload": payload, "created_at": stamp(), "by": by, "reason": reason}
            receipt["preservation_id"] = receipt_key(receipt)
            validate_receipt(receipt)
            _publish_or_reuse(root, temporary, receipt)
            verify_copy(receipt["preservation_id"], confirm=True)
            return load_receipt(receipt["preservation_id"])
        finally:
            remove_owned_temporary(temporary, created)


def _publish_or_reuse(root: Path, temporary: Path, receipt: dict) -> None:
    key = receipt["preservation_id"]
    existing = read_receipt(receipt_path(key))
    if existing is not None:
        if (existing["by"], existing["reason"]) != (receipt["by"], receipt["reason"]):
            raise ValueError("preservation receipt audit cannot be rewritten")
        verify_copy(key, confirm=True)  # refusal preserves any changed/missing old payload, never overwrites it
        return
    _capacity(root)
    publish(temporary, receipt, root)


def verify_copy(key: str, *, confirm: bool = False) -> dict:
    if confirm is not True:
        raise ValueError("preservation byte verification needs explicit --confirm; no payload was read")
    value = load_receipt(key)
    _private_root(preservation_root())
    path = checked_path(preservation_root()) / (key + ".blob")
    from .task_retention_store import observed_stat
    info = observed_stat(path)
    if info["mode"] & 0o077:
        raise ValueError("preserved payload is not private")
    payload = copy_stream(path, None, info)
    if payload != value["payload"] or load_receipt(key) != value:
        raise ValueError("preserved copy or receipt does not match its original recorded bytes")
    return {"schema_version": 1, "preservation_id": key, "resource_id": value["resource_id"],
        "verified_at": stamp(), "payload": payload, "verification": "recorded_copy_bytes_matching",
        "basis": "explicit_full_copy_hash_not_current_source_or_complete_recovery_verification",
        "current_source_checked": False, "recoverable_archive_verified": False,
        "cleanup_authority": "not_granted", "grants_execution": False,
        "note": "matching copy bytes do not authorize source removal, restore, merge or upload"}


def preservation_views(resources: list) -> list[str]:
    selected = {row["resource_id"]: row for row in resources}
    notes = []
    for row in resources:
        row["preservation"] = {"receipts": [], "omitted_receipts": 0, "observed_receipts": 0,
                               "basis": "receipt_metadata_only_payload_not_reread_at_query", "complete_archive_verified": False}
    try:
        root = checked_path(preservation_root())
        if not root.is_dir():
            return notes
        _private_root(root)
        for number, path in enumerate(root.iterdir()):
            if number >= MAX_RECEIPTS * 2 + 16:
                notes.append("preservation discovery bounded; missing receipts do not establish missing copies")
                break
            if path.suffix != ".json":
                continue
            try:
                value = load_receipt(path.stem)
                if (value["resource_id"] in selected and
                        value["source_id"] == selected[value["resource_id"]]["source_id"] and
                        value["resource_version"] <= selected[value["resource_id"]]["version"]):
                    projection = selected[value["resource_id"]]["preservation"]
                    projection["observed_receipts"] += 1
                    if len(projection["receipts"]) < 8:
                        projection["receipts"].append(_public(value))
                    else:
                        projection["omitted_receipts"] += 1
            except (ValueError, OSError, TypeError) as exc:
                notes.append("preservation receipt invalid: " + display(str(exc), 512))
    except (ValueError, OSError) as exc:
        notes.append("preservation receipts unavailable: " + display(str(exc), 512))
    return notes


def _private_root(root: Path) -> None:
    info = checked_path(root).stat()
    if not hasattr(os, "geteuid") or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("preservation storage is not an owned private directory")


def _public(value: dict) -> dict:
    return {"preservation_id": value["preservation_id"], "resource_version": value["resource_version"],
            "payload": value["payload"], "created_at": value["created_at"],
            "by": display(value["by"], 128), "reason": display(value["reason"], 256),
            "current_payload_reverified": False, "current_source_reverified": False,
            "scope": "one_recorded_file_copy_not_complete_directory_or_worktree_archive"}
