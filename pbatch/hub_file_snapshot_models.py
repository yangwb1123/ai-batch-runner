"""Strict, bounded inspection snapshots; hashes establish byte integrity only."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata

from .hub_store_helpers import identifier
from .memory_records import redact_content
from .workspace_bundle import validate_bundle, validate_outputs

SCHEMA = "abr.file-snapshots.v1"
READ_SCOPE = "agent.artifacts:read"
MAX_PATHS = 8
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024
MAX_WIRE_BYTES = 512 * 1024
VERIFICATION = "content_integrity_only"
UNAVAILABLE_CODES = frozenset({
    "missing", "unsafe_path", "not_regular_file", "too_large", "read_failed",
    "read_timeout", "changed", "secret_detected", "turn_not_completed",
    "cancel_requested", "capture_unavailable", "capacity_exceeded",
})
_PRIVATE_PARTS = frozenset({
    ".git", ".ssh", ".aws", ".azure", ".config", ".gnupg", ".kube",
    ".abr", ".codex", ".claude", ".gemini", ".netrc", ".npmrc", ".pypirc",
    ".docker", ".git-credentials", ".vault-token", ".bash_history", ".zsh_history", ".python_history",
})
_PRIVATE_NAME = re.compile(
    r"^(?:\.env(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\..*)?|"
    r"(?:credentials?|secrets?|tokens?|passwords?)(?:\..*)?)$", re.I)
_PRIVATE_SUFFIX = (".pem", ".key", ".p12", ".pfx", ".keystore")
_FILE_KEYS = {"snapshot_id", "path", "content_b64", "sha256", "size_bytes"}
_UPLOAD_KEYS = {"schema", "instance_id", "generation", "session_id", "turn_id",
                "snapshot_set_id", "files", "unavailable"}


def opaque_id(value, label="snapshot_id"):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError(f"invalid {label}")
    return value


def validate_snapshot_paths(paths):
    """Validate exact portable file paths, preserving the declared order."""
    if not isinstance(paths, list) or len(paths) > MAX_PATHS:
        raise ValueError("snapshot paths exceed the declared path limit")
    validate_outputs(paths)
    for path in paths:
        if any(unicodedata.category(char) == "Cf" for char in path):
            raise ValueError("snapshot path contains invisible formatting")
        if redact_content(path) != path or any(_private_part(part) for part in path.split("/")):
            raise ValueError("snapshot path is private")
    return list(paths)


def _private_part(part):
    value = part.casefold()
    return value in _PRIVATE_PARTS or _PRIVATE_NAME.fullmatch(value) or value.endswith(_PRIVATE_SUFFIX)


def rejects_secret_content(raw: bytes) -> bool:
    """Recognized credentials reject a snapshot; this is not a secret-free proof."""
    text = raw.decode("utf-8", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    # ASCII secrets in NUL-padded binary encodings must not evade this policy.
    text = text.replace("\x00", "")
    return (bool(re.search(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----", text))
            or redact_content(text) != text)


def content_kind(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return "binary"
    if any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in text):
        return "binary"
    return "text"


def validate_snapshot_file(row):
    if not isinstance(row, dict) or set(row) != _FILE_KEYS:
        raise ValueError("invalid snapshot file shape")
    opaque_id(row["snapshot_id"])
    validate_snapshot_paths([row["path"]])
    from .workspace_bundle import SCHEMA as bundle_schema
    validate_bundle({"schema": bundle_schema, "files": [
        {"path": row["path"], "content_b64": row["content_b64"]}]})
    raw = base64.b64decode(row["content_b64"], validate=True)
    if type(row["size_bytes"]) is not int or row["size_bytes"] != len(raw):
        raise ValueError("snapshot size does not match its bytes")
    if row["sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValueError("snapshot digest does not match its bytes")
    if rejects_secret_content(raw):
        raise ValueError("snapshot content contains a recognized credential")
    return dict(row)


def _unavailable(rows):
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "code"}:
            raise ValueError("invalid unavailable snapshot shape")
        if not isinstance(row["code"], str) or row["code"] not in UNAVAILABLE_CODES:
            raise ValueError("invalid unavailable snapshot code")
        result.append(dict(row))
    return result


def validate_upload(body):
    """Validate the Gateway DTO without normalizing or redacting its bytes."""
    if not isinstance(body, dict) or set(body) != _UPLOAD_KEYS or body["schema"] != SCHEMA:
        raise ValueError("invalid file snapshot upload schema")
    for name in ("instance_id", "session_id", "turn_id"):
        identifier(body[name], name)
    if type(body["generation"]) is not int or not 1 <= body["generation"] <= 9999999999:
        raise ValueError("invalid snapshot generation")
    opaque_id(body["snapshot_set_id"], "snapshot_set_id")
    files, missing = body["files"], body["unavailable"]
    if not isinstance(files, list) or not isinstance(missing, list):
        raise ValueError("snapshot entries must be lists")
    if not 1 <= len(files) + len(missing) <= MAX_PATHS:
        raise ValueError("snapshot set must contain bounded declared paths")
    files, missing = [validate_snapshot_file(row) for row in files], _unavailable(missing)
    validate_snapshot_paths([row["path"] for row in files + missing])
    if len({row["snapshot_id"] for row in files}) != len(files):
        raise ValueError("duplicate snapshot ID")
    if sum(row["size_bytes"] for row in files) > MAX_TOTAL_BYTES:
        raise ValueError("snapshot set exceeds the total byte limit")
    result = dict(body, files=files, unavailable=missing)
    if len(canonical_bytes(result)) > MAX_WIRE_BYTES:
        raise ValueError("snapshot upload exceeds the wire byte limit")
    return result


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def file_metadata(row):
    result = {key: row[key] for key in ("snapshot_id", "path", "sha256", "size_bytes")}
    result["content_kind"] = content_kind(base64.b64decode(row["content_b64"], validate=True))
    return result
