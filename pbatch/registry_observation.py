"""Bounded reads of an explicitly chosen existing process-registry directory.

No directory creation, write probe, lock, prune, argv rendering or process
control. Sequential stat checks do not establish an atomic snapshot or lease.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .instance_observation_io import (_constant, _finite_float, _identity,
                                      _object, _owned_regular)
from .task_center_io import _open_regular, checked_path

MAX_BYTES = 16 * 1024
MAX_ENTRIES = 256
MAX_RECORDS = 64
_CONTROLS = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f"
                       r"\u202a-\u202e\u2066-\u2069]")


def literal(value: object, maximum: int = 240) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    text = str(value).encode("utf-8", errors="backslashreplace").decode("utf-8")
    return _CONTROLS.sub(" ", text)[:maximum]


def read_record(path: Path) -> dict:
    with _open_regular(path) as handle:
        before = os.fstat(handle.fileno())
        _owned_regular(before)
        raw = handle.read(MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
        named = os.stat(checked_path(path), follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(named):
            raise ValueError("registry record changed during read")
    if len(raw) > MAX_BYTES:
        raise ValueError("registry record byte budget exhausted")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=_constant, parse_float=_finite_float)
        # UTF-8 source bytes alone do not reject JSON-escaped lone surrogates.
        # Validate keys and nested values too, without logging any private field.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid registry metadata") from exc
    if not isinstance(value, dict):
        raise ValueError("registry metadata must be an object")
    return value


def scan(root: Path, *, maximum: int = MAX_RECORDS) -> tuple[list, dict]:
    """Return selected path/object pairs for the caller's closed projection."""
    info = {"status": "not_read", "available": False, "truncated": False, "malformed": 0,
            "entries_observed": 0, "records": 0, "error": ""}
    if maximum <= 0:
        info["truncated"] = True
        return [], info
    before = None
    try:
        root = checked_path(root)
        before = _directory(root)
        records = _scan(root, min(maximum, MAX_RECORDS), info)
        if before != _directory(checked_path(root)):
            raise ValueError("registry directory changed during read")
        info.update(status="observed", available=True)
        return records, info
    except FileNotFoundError:
        info["status"] = "missing" if before is None else "unavailable"
        if before is not None:
            info["error"] = "process_registry_unavailable"
        return [], info
    except (OSError, ValueError):
        info.update(status="unavailable", error="process_registry_unavailable")
        return [], info


def _directory(root: Path) -> tuple:
    info = os.stat(root, follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or not hasattr(os, "geteuid") or
            info.st_uid != os.geteuid()):
        raise ValueError("registry directory owner or type unavailable")
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode


def _scan(root: Path, maximum: int, info: dict) -> list:
    records = []
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_ENTRIES or len(records) >= maximum:
                info["truncated"] = True
                break
            info["entries_observed"] += 1
            path = Path(entry.path)
            if path.suffix != ".json":
                continue
            try:
                value = read_record(path)
            except (OSError, ValueError):
                info["malformed"] += 1
                continue
            records.append((path, value))
    info["records"] = len(records)
    return sorted(records, key=lambda pair: pair[0].name)


def diagnostics(info: dict) -> list[str]:
    result = []
    if info["error"]:
        result.append("process registry unavailable; no cached facts retained")
    if info["truncated"]:
        result.append("process registry scan limited; omissions unknown")
    if info["malformed"]:
        result.append("process registry has refused metadata; missing facts are not zero")
    return result
