"""Private source catalog: default runtimes plus explicitly registered paths.

No /tmp or repository crawl. An operator registers custom runtimes once;
HTTP consumers cannot supply a filesystem path. Registration is metadata only.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from . import file_lock
from .task_center_io import checked_path, display, read_object
from .user_dirs import pbatch_runtime_home

SCHEMA_VERSION = 1
MAX_SOURCES = 64
MAX_CATALOG_BYTES = 128 * 1024
_FIELDS = {"source_id", "name", "data_dir", "workspace", "campaign_state", "events_file"}


def catalog_path() -> Path:
    return pbatch_runtime_home() / "ops" / "task-sources.json"


def source(name: str, data_dir: str, workspace: str = "",
           campaign_state: str = "", events_file: str = "") -> dict:
    root = checked_path(data_dir)
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise ValueError("source name must contain 1 through 128 characters")
    return {
        "source_id": "src-" + hashlib.sha256(str(root).encode()).hexdigest()[:24],
        "name": display(name, 128), "data_dir": str(root),
        "workspace": str(checked_path(workspace)) if workspace else "",
        "campaign_state": str(checked_path(campaign_state)) if campaign_state else
                          str(root / "campaign" / "state.jsonl"),
        "events_file": str(checked_path(events_file)) if events_file else str(root / "events.jsonl"),
    }


def registered_sources() -> list[dict]:
    value = read_object(catalog_path(), MAX_CATALOG_BYTES)
    if value is None:
        return []
    if (set(value) != {"schema_version", "sources"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != SCHEMA_VERSION):
        raise ValueError("unsupported task source catalog schema")
    rows = value["sources"]
    if not isinstance(rows, list) or len(rows) > MAX_SOURCES:
        raise ValueError("invalid task source catalog size")
    checked = [_validate(item) for item in rows]
    if len({item["source_id"] for item in checked}) != len(checked):
        raise ValueError("duplicate task source identity")
    return checked


def _validate(item: dict) -> dict:
    if not isinstance(item, dict) or set(item) != _FIELDS:
        raise ValueError("invalid task source fields")
    if not all(isinstance(value, str) for value in item.values()):
        raise ValueError("task source fields must be strings")
    if not all(Path(item[key]).is_absolute() for key in
               ("data_dir", "campaign_state", "events_file")):
        raise ValueError("task source paths must be absolute")
    if item["workspace"] and not Path(item["workspace"]).is_absolute():
        raise ValueError("workspace path must be absolute")
    normalized = source(item["name"], item["data_dir"], item["workspace"],
                        item["campaign_state"], item["events_file"])
    if normalized != item:
        raise ValueError("task source identity/path mismatch")
    return normalized


def observation_roots() -> list[Path]:
    """Read-only compatibility, disabled by any explicit runtime boundary."""
    primary = pbatch_runtime_home()
    roots = [primary]
    if not any(os.environ.get(key, "").strip() for key in
               ("PBATCH_RUNTIME_HOME", "PBATCH_HOME", "CODEX_HOME")):
        legacy = primary.parent / ".codex" / "pbatch"
        if legacy != primary:
            roots.append(legacy)
    return roots


def discover_sources() -> tuple[list[dict], list[str]]:
    """Bounded default enumeration; explicit registrations override defaults."""
    rows = {item["source_id"]: item for item in registered_sources()}
    notes = []
    for home in observation_roots():
        try:
            _discover_root(home / "projects", rows, notes)
        except (OSError, ValueError) as exc:
            notes.append(display(str(exc)))
    return list(rows.values()), notes


def _discover_root(root: Path, rows: dict, notes: list) -> None:
    root = checked_path(root)
    if not root.is_dir():
        return
    candidates = []
    for visited, child in enumerate(root.iterdir()):
        if visited >= MAX_SOURCES * 4 or len(candidates) >= MAX_SOURCES:
            notes.append("default workspace discovery limited to 64 directory / 256 scanned entries")
            break
        if child.is_dir() and not child.is_symlink():
            candidates.append(child)
    for child in sorted(candidates):
        item = source(child.name[:128], str(child))
        if len(rows) >= MAX_SOURCES and item["source_id"] not in rows:
            notes.append("source limit reached; narrow registered sources")
            break
        rows.setdefault(item["source_id"], item)


def register_source(item: dict) -> dict:
    item = _validate(item)
    if not Path(item["data_dir"]).is_dir():
        raise ValueError("registered data directory must already exist")
    if item["workspace"] and not Path(item["workspace"]).is_dir():
        raise ValueError("registered workspace must already exist")
    return _change_catalog(item, "")


def unregister_source(source_id: str) -> dict:
    """Remove a registration only; never remove a directory or a task."""
    return _change_catalog(None, source_id)


def _change_catalog(item: dict | None, remove: str) -> dict:
    path = checked_path(catalog_path())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.with_suffix(".lock"), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("task source lock must be a regular file")
        file_lock.flock(descriptor, file_lock.LOCK_EX)
        rows = {row["source_id"]: row for row in registered_sources()}
        if item:
            previous = rows.get(item["source_id"])
            if previous is not None and previous != item:
                raise ValueError("data directory already registered; remove its registration first")
            rows[item["source_id"]] = item
        elif remove not in rows:
            raise ValueError("registered source not found")
        else:
            del rows[remove]
        if len(rows) > MAX_SOURCES:
            raise ValueError("too many registered task sources")
        _write_catalog(path, list(rows.values()))
        return item or {"removed": remove}
    finally:
        os.close(descriptor)


def _write_catalog(path: Path, rows: list[dict]) -> None:
    value = {"schema_version": SCHEMA_VERSION, "sources": rows}
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_CATALOG_BYTES:
        raise ValueError("task source catalog exceeds its byte budget")
    descriptor, temporary = tempfile.mkstemp(prefix=".task-sources-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
