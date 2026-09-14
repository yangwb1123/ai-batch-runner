"""Canonical, bounded workspace bundle DTO and path validation."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from pathlib import PurePosixPath, PureWindowsPath

SCHEMA = "pbatch.workspace.v1"
MAX_FILES = 128
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024
MAX_BUNDLE_BYTES = 512 * 1024
MAX_PATH_BYTES = 512
MAX_SEGMENT_BYTES = 255
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?$", re.I)
_WINDOWS_FORBIDDEN = set('<>:"|?*')


class WorkspaceBundleError(ValueError):
    """Raised when workspace bundle data or paths are unsafe or unbounded."""


def _path_parts(value: object) -> tuple[str, ...]:
    """Validate a portable relative POSIX file path and return its parts."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise WorkspaceBundleError("workspace path must be a relative POSIX path")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkspaceBundleError("workspace path is not valid UTF-8") from exc
    if len(encoded) > MAX_PATH_BYTES or unicodedata.normalize("NFC", value) != value:
        raise WorkspaceBundleError("workspace path is non-canonical or too long")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise WorkspaceBundleError("workspace path contains control characters")
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise WorkspaceBundleError("workspace path must be relative")
    parts = tuple(value.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise WorkspaceBundleError("workspace path contains an unsafe segment")
    for part in parts:
        if (len(part.encode("utf-8")) > MAX_SEGMENT_BYTES
                or part[-1:] in (".", " ")
                or any(char in _WINDOWS_FORBIDDEN for char in part)
                or _WINDOWS_RESERVED.fullmatch(part)):
            raise WorkspaceBundleError("workspace path contains a non-portable segment")
    return parts


def _validate_path_set(paths: list[str]) -> list[str]:
    """Reject duplicate, case-folded, and file/parent path collisions."""
    normalized = sorted(paths)
    folded = {path.casefold(): path for path in normalized}
    if len(folded) != len(normalized):
        raise WorkspaceBundleError("workspace paths collide case-insensitively")
    keys = set(folded)
    for path in normalized:
        parts = path.casefold().split("/")
        if any("/".join(parts[:index]) in keys
               for index in range(1, len(parts))):
            raise WorkspaceBundleError("workspace file paths have a parent conflict")
    return normalized


def validate_bundle(bundle: object) -> dict:
    """Return a canonical DTO after strict shape, base64, and size checks."""
    files = _bundle_files(bundle)
    normalized, total = [], 0
    for item in files:
        row, size = _normalize_file(item)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise WorkspaceBundleError("workspace bundle exceeds the size limit")
        normalized.append(row)
    _validate_path_set([item["path"] for item in normalized])
    normalized.sort(key=lambda item: item["path"])
    result = {"schema": SCHEMA, "files": normalized}
    if len(_canonical_json(result)) > MAX_BUNDLE_BYTES:
        raise WorkspaceBundleError("canonical workspace bundle exceeds the size limit")
    return result


def _bundle_files(bundle: object) -> list:
    if not isinstance(bundle, dict) or set(bundle) != {"schema", "files"}:
        raise WorkspaceBundleError("workspace bundle has an invalid shape")
    if bundle.get("schema") != SCHEMA:
        raise WorkspaceBundleError("unsupported workspace bundle schema")
    files = bundle.get("files")
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise WorkspaceBundleError("workspace bundle file count exceeds the limit")
    return files


def _normalize_file(item: object) -> tuple[dict, int]:
    if not isinstance(item, dict) or set(item) != {"path", "content_b64"}:
        raise WorkspaceBundleError("workspace file has an invalid shape")
    path = item.get("path")
    _path_parts(path)
    encoded = item.get("content_b64")
    if isinstance(encoded, str) and len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
        raise WorkspaceBundleError("workspace file exceeds the size limit")
    if not isinstance(encoded, str) or not encoded.isascii():
        raise WorkspaceBundleError("workspace content must be canonical base64")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise WorkspaceBundleError("workspace content must be canonical base64") from exc
    if base64.b64encode(content).decode("ascii") != encoded:
        raise WorkspaceBundleError("workspace content must be canonical base64")
    if len(content) > MAX_FILE_BYTES:
        raise WorkspaceBundleError("workspace file exceeds the size limit")
    return {"path": path, "content_b64": encoded}, len(content)


def _canonical_json(value: dict) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise WorkspaceBundleError("workspace bundle is not canonical JSON") from exc


def bundle_bytes(bundle: object) -> bytes:
    """Serialize a validated bundle using its single canonical representation."""
    return _canonical_json(validate_bundle(bundle))


def bundle_digest(bundle: object) -> str:
    """Return the full SHA-256 digest of canonical bundle bytes."""
    return hashlib.sha256(bundle_bytes(bundle)).hexdigest()


def validate_outputs(paths: object) -> list[str]:
    """Validate a bounded list of exact output-file paths and sort it."""
    if not isinstance(paths, list) or len(paths) > MAX_FILES:
        raise WorkspaceBundleError("workspace outputs must be a bounded path list")
    for path in paths:
        _path_parts(path)
    return _validate_path_set(paths)
