"""Private, bounded observation windows; never create/probe/prune runtime data.

Stat comparisons are sequential change checks, not atomic snapshots or hostile
same-uid isolation. Missing, damaged and partial windows remain distinguishable.
"""
from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path

from .task_center_io import _open_regular, checked_path

MAX_BYTES = 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
MAX_RECORDS = 512
MAX_DIRECTORY_ENTRIES = 800


def _identity(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _owned_regular(info) -> None:
    if not hasattr(os, "geteuid") or info.st_uid != os.geteuid():
        raise ValueError("unknown_or_foreign_ledger_owner")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("unsupported_ledger_type_or_alias")


def _tail(path: Path, info: dict) -> bytes:
    with _open_regular(path) as handle:
        before = os.fstat(handle.fileno())
        _owned_regular(before)
        start = max(0, before.st_size - MAX_BYTES)
        aligned = True
        if start:
            handle.seek(start - 1)
            aligned = handle.read(1) == b"\n"
            info["bytes_read"] += 1
        raw = handle.read(MAX_BYTES)
        info["bytes_read"] += len(raw)
        after = os.fstat(handle.fileno())
        try:
            named = os.stat(checked_path(path), follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ValueError("ledger_changed_during_read") from exc
        if _identity(before) != _identity(after) or _identity(after) != _identity(named):
            raise ValueError("ledger_changed_during_read")
    info.update(available=True, status="observed", truncated=bool(start))
    return raw if aligned else raw.partition(b"\n")[2]


def _constant(_value):
    raise ValueError("nonfinite JSON")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("overflowing JSON number")
    return number


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode(line: bytes) -> dict | None:
    if len(line) > MAX_LINE_BYTES:
        return None
    try:
        value = json.loads(line.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=_constant, parse_float=_finite_float)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError, RecursionError):
        return None


def _lines(raw: bytes, maximum: int, info: dict) -> list:
    lines = raw.split(b"\n")
    info["incomplete"] = bool(lines[-1])
    lines.pop()  # Only newline-completed records are observations.
    info["truncated"] |= len(lines) > maximum
    selected = lines[-maximum:]
    info["lines"] = len(selected)
    records = []
    for line in selected:
        value = _decode(line) if line.strip() else None
        if value is None:
            info["malformed"] += 1
        else:
            info["records"] += 1
        records.append(value)  # Keep gaps: two failures across damage are not consecutive.
    return records


def read_window(path: Path, limit: int) -> tuple[list, dict]:
    """At most 1 MiB + one alignment byte, 512 lines, 256 KiB per line."""
    info = {"status": "missing", "available": False, "truncated": False,
            "malformed": 0, "incomplete": False, "records": 0,
            "lines": 0, "bytes_read": 0, "error": ""}
    if limit <= 0:
        info.update(status="not_read", truncated=True)
        return [], info
    try:
        raw = _tail(path, info)
        return _lines(raw, min(limit, MAX_RECORDS), info), info
    except FileNotFoundError:
        return [], info
    except (OSError, ValueError):
        info.update(status="unavailable", available=False, error="ledger_read_unavailable")
        return [], info


def window_clear(info: dict) -> bool:
    return (info["available"] and not info["truncated"] and not info["malformed"]
            and not info["incomplete"] and not info["error"])


def project_window(root: Path, limit: int) -> tuple[list[Path], dict]:
    """Bound enumeration independently of the displayed workspace limit."""
    info = {"available": False, "truncated": False, "entries_observed": 0,
            "directories_observed": 0, "malformed": 0, "error": ""}
    if limit <= 0:
        info["truncated"] = True
        return [], info
    try:
        paths = _directories(checked_path(root), info)
        info["available"] = True
        maximum = min(limit, 200)
        info["truncated"] |= len(paths) > maximum
        return sorted(paths)[:maximum], info
    except FileNotFoundError:
        return [], info
    except (OSError, ValueError):
        info["error"] = "workspace_directory_unavailable"
        return [], info


def _directories(root: Path, info: dict) -> list[Path]:
    found = []
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_DIRECTORY_ENTRIES:
                info["truncated"] = True
                break
            info["entries_observed"] += 1
            if entry.is_symlink():
                info["malformed"] += 1
            elif entry.is_dir(follow_symlinks=False):
                if _safe_name(entry.name):
                    found.append(Path(entry.path))
                    info["directories_observed"] += 1
                else:
                    info["malformed"] += 1
    return found


def _safe_name(name: str) -> bool:
    try:
        encoded = name.encode("utf-8", "strict")
    except UnicodeError:
        return False
    forbidden = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    return len(encoded) <= 256 and not any(
        ord(char) < 32 or 127 <= ord(char) <= 159 or char in forbidden for char in name)
