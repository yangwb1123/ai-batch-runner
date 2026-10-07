"""Closed preservation metadata. Review dates and OS stat fields grant no disposal right."""
from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from datetime import datetime
from pathlib import Path, PurePosixPath

from .task_goal_schema import text
from .task_lifecycle import RUN_PATTERN

RESOURCE_PATTERN = re.compile(r"rr-[a-f0-9]{32}\Z")
SOURCE_PATTERN = re.compile(r"src-[a-f0-9]{24}\Z")
MAX_RESOURCES = 64
MAX_BYTES = 32 * 1024
MAX_AUDIT = 16
PIN_RESERVE = 4096
REVIEW_SECONDS = 72 * 3600
KINDS = {"file", "directory", "worktree"}
_FIELDS = {"schema_version", "resource_id", "source_id", "source_root", "source_digest", "relative_path", "kind",
           "run_id", "registered_at", "review_at", "version", "pinned", "registration_stat", "root_identity", "audit"}
_STAT = {"device", "inode", "uid", "mode", "bytes", "mtime_ns", "ctime_ns"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def resource_key(source_id: str, relative: str) -> str:
    return "rr-" + digest([source_id, relative])[:32]


def instant(value: str) -> float:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError("retention time must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        result = parsed.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("invalid retention timestamp") from exc
    if parsed.tzinfo is None or not math.isfinite(result):
        raise ValueError("retention time needs an explicit timezone")
    return result


def relative_path(value: str) -> None:
    if (not isinstance(value, str) or not value or len(value) > 2048 or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid retention relative resource path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in value.split("/")) or str(path) != value:
        raise ValueError("retention resource must be a canonical strict descendant, not a source root")


def _fields(value, allowed: set) -> None:
    if not isinstance(value, dict) or set(value) != allowed:
        raise ValueError("invalid retention metadata fields")


def _integer(value, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value < 2 ** 128:
        raise ValueError("invalid retention integer")


def _identity_fields(value: dict) -> None:
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported retention schema")
    if not isinstance(value["source_id"], str) or not SOURCE_PATTERN.fullmatch(value["source_id"]):
        raise ValueError("invalid retention source identity")
    relative_path(value["relative_path"])
    root = value["source_root"]
    if not isinstance(root, str) or not Path(root).is_absolute() or str(Path(root)) != root:
        raise ValueError("retention source root must be canonical and absolute")
    if value["source_id"] != "src-" + hashlib.sha256(root.encode()).hexdigest()[:24]:
        raise ValueError("retention source root identity mismatch")
    if value["resource_id"] != resource_key(value["source_id"], value["relative_path"]):
        raise ValueError("retention resource identity mismatch")
    if not isinstance(value["source_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["source_digest"]):
        raise ValueError("invalid retention source digest")
    if not isinstance(value["kind"], str) or value["kind"] not in KINDS or type(value["pinned"]) is not bool:
        raise ValueError("invalid retention kind or protection marker")
    if not isinstance(value["run_id"], str) or (value["run_id"] and not RUN_PATTERN.fullmatch(value["run_id"])):
        raise ValueError("invalid retention run association")


def validate_state(value: dict) -> None:
    _fields(value, _FIELDS)
    _identity_fields(value)
    _fields(value["registration_stat"], _STAT)
    _fields(value["root_identity"], {"device", "inode", "uid", "mode"})
    for info in (value["registration_stat"], value["root_identity"]):
        for number in info.values():
            _integer(number)
    if (not stat.S_ISDIR(value["root_identity"]["mode"]) or
            stat.S_ISREG(value["registration_stat"]["mode"]) != (value["kind"] == "file") or
            not (stat.S_ISREG(value["registration_stat"]["mode"]) or stat.S_ISDIR(value["registration_stat"]["mode"]))):
        raise ValueError("retention stat/type declaration mismatch")
    _integer(value["version"], 1)
    _audit(value)
    encoded(value)


def _audit(value: dict) -> None:
    rows = value["audit"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_AUDIT or len(rows) != value["version"]:
        raise ValueError("retention audit/version mismatch")
    review, last_at, pinned = instant(value["registered_at"]) + REVIEW_SECONDS, float("-inf"), False
    for version, row in enumerate(rows, 1):
        _fields(row, {"version", "kind", "by", "reason", "at", "review_at"})
        if type(row["version"]) is not int or row["version"] != version or pinned:
            raise ValueError("invalid retention audit order or update after permanent pin")
        if not text(row["by"], 128) or not text(row["reason"], 256):
            raise ValueError("retention audit needs actor label and reason")
        at, current = instant(row["at"]), instant(row["review_at"])
        if at < last_at:
            raise ValueError("retention audit timestamp moved backwards")
        review, pinned = _transition(value, row, version, review, current, at)
        last_at = at
    if instant(value["review_at"]) != review or value["pinned"] != pinned:
        raise ValueError("retention policy/audit mismatch")
    if not pinned and len(rows) >= MAX_AUDIT:
        raise ValueError("retention audit must reserve room for permanent protection")


def _transition(value: dict, row: dict, version: int, review: float, current: float, at: float) -> tuple:
    if version == 1:
        if row["kind"] != "registered" or row["at"] != value["registered_at"] or current != review:
            raise ValueError("initial retention review must be registration plus 72 hours")
        return review, False
    if row["kind"] == "extended":
        if current <= max(review, at) or version >= MAX_AUDIT:
            raise ValueError("retention extension cannot shorten or consume the final protection slot")
        return current, False
    if row["kind"] == "pinned" and current == review:
        return review, True
    raise ValueError("invalid retention audit transition")


def encoded(value: dict) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_BYTES - (0 if value.get("pinned") is True else PIN_RESERVE):
        raise ValueError("retention metadata byte budget exhausted; protection room is reserved")
    return raw


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate retention JSON field")
        value[key] = item
    return value


def decode(raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise ValueError("retention record byte budget exceeded")
    def nonfinite(_):
        raise ValueError("nonfinite retention JSON")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid retention JSON") from exc
    validate_state(value)
    return value
