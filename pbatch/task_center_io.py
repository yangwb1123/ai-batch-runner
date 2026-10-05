"""Bounded, symlink-rejecting local reads for the task center.

Readers never create directories, lock files, or repair malformed ledgers.
Only explicit source registration writes the private source catalog.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .memory_records import redact

MAX_BYTES = 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
MAX_RECORDS = 512
_CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x1f\x7f]")


def display(value: object, maximum: int = 240) -> str:
    """Redact before clipping, and never render terminal control sequences."""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return _CONTROL.sub(" ", redact(str(value)))[:maximum]


def checked_path(value: str | Path) -> Path:
    path = Path(os.path.abspath(Path(value).expanduser()))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("task source path contains a symlink")
    return path


def _open_regular(path: Path):
    path = checked_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("task source must be a regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def read_object(path: Path, maximum: int = MAX_BYTES) -> dict | None:
    try:
        with _open_regular(path) as handle:
            raw = handle.read(maximum + 1)
    except FileNotFoundError:
        return None
    if len(raw) > maximum:
        raise ValueError("task source catalog exceeds its byte budget")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid task source catalog JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("task source catalog must be an object")
    return value


def read_rows(path: Path) -> tuple[list[dict], dict]:
    """Read complete JSONL records in a bounded tail; expose missing coverage."""
    info = {"path": str(path), "available": False, "truncated": False,
            "malformed": 0, "incomplete": False, "records": 0, "error": ""}
    try:
        with _open_regular(path) as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - MAX_BYTES)
            if start:
                handle.seek(start - 1)
                aligned = handle.read(1) == b"\n"
            else:
                aligned = True
            raw = handle.read(MAX_BYTES)
        info.update(available=True, truncated=bool(start))
        if not aligned:
            raw = raw.partition(b"\n")[2]
        return _decode_rows(raw, info), info
    except FileNotFoundError:
        return [], info
    except (OSError, ValueError) as exc:
        info["error"] = display(str(exc))
        return [], info


def _decode_rows(raw: bytes, info: dict) -> list[dict]:
    lines = raw.split(b"\n")
    info["incomplete"] = bool(lines[-1])
    lines = lines[:-1]  # a concurrently appended, unfinished line is not a fact
    info["truncated"] |= len(lines) > MAX_RECORDS
    records = []
    for line in lines[-MAX_RECORDS:]:
        if not line.strip():
            continue
        if len(line) > MAX_LINE_BYTES:
            info["malformed"] += 1
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            records.append(value)
        except (ValueError, UnicodeError, RecursionError):
            info["malformed"] += 1
    info["records"] = len(records)
    return records
