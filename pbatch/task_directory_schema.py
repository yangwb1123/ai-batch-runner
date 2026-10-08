"""Closed bounded directory-byte copy profile; never restoration/disposal authority."""
from __future__ import annotations

from pathlib import PurePosixPath
import re

from .task_retention_schema import _fields, _integer, digest

METHOD = "bounded-directory-bytes-v1"
FORMAT = "canonical-uncompressed-ustar-v1"
MAX_ENTRIES = 256  # includes the selected root and every empty directory
MAX_DEPTH = 16
MAX_PATH_BYTES = 99  # USTAR name field, reserving a directory's trailing slash
_SUMMARY = {"method", "format", "entries", "files", "directories", "content_bytes", "members_sha256"}
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def member_path(value: str) -> str:
    if not isinstance(value, str) or not value or _CONTROLS.search(value):
        raise ValueError("directory copy needs a literal safe relative member path")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError("directory copy member name is not strict UTF-8") from exc
    parts = value.split("/")
    if (size > MAX_PATH_BYTES or len(parts) > MAX_DEPTH or "\\" in value or ":" in value or
            value != PurePosixPath(value).as_posix() or
            (value != "." and any(part in {"", ".", "..", ".git"} for part in parts))):
        raise ValueError("directory copy path/Git marker outside bounded USTAR profile")
    return value


def summarize(members: list[dict]) -> dict:
    files = sum(row["kind"] == "file" for row in members)
    return {"method": METHOD, "format": FORMAT, "entries": len(members), "files": files,
            "directories": len(members) - files,
            "content_bytes": sum(row["bytes"] for row in members), "members_sha256": digest(members)}


def validate_summary(value: dict, payload_bytes: int) -> None:
    _fields(value, _SUMMARY)
    if value["method"] != METHOD or value["format"] != FORMAT:
        raise ValueError("unsupported directory preservation profile")
    for key in ("entries", "files", "directories", "content_bytes"):
        _integer(value[key])
    if (not 1 <= value["entries"] <= MAX_ENTRIES or value["directories"] < 1 or
            value["entries"] != value["files"] + value["directories"] or
            value["content_bytes"] > payload_bytes or payload_bytes < 10240 or payload_bytes % 10240):
        raise ValueError("directory preservation count/byte budget conflict")
    if not isinstance(value["members_sha256"], str) or not _HASH.fullmatch(value["members_sha256"]):
        raise ValueError("invalid directory preservation member digest")
