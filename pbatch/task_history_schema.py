"""Closed, versioned audit metadata for supported native user-history adapters."""
from __future__ import annotations

import re

DIGEST_PATTERN = re.compile(r"[a-f0-9]{64}\Z")

MAX_NATIVE_BYTES = 2 * 1024 * 1024
MAX_NATIVE_LINE_BYTES = 128 * 1024
MAX_NATIVE_RECORDS = 2048
NATIVE_FORMATS = {"pi-session", "codex-session"}
_FIELDS = {"schema_version", "format", "session_digest", "workspace_digest", "selection",
           "leaf_digest", "records", "user_messages", "excluded_user_messages", "excluded_records"}


def validate_adapter(value: dict, message_count: int) -> None:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("invalid native history adapter audit fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported native history adapter audit version")
    format = value["format"]
    if not isinstance(format, str) or format not in NATIVE_FORMATS:
        raise ValueError("unsupported native history adapter format")
    for field in ("session_digest", "workspace_digest"):
        if not isinstance(value[field], str) or not DIGEST_PATTERN.fullmatch(value[field]):
            raise ValueError("invalid native history adapter identity digest")
    expected = "pi_branch_users" if format == "pi-session" else "codex_user_events"
    if value["selection"] != expected:
        raise ValueError("invalid native history selection basis")
    leaf = value["leaf_digest"]
    if not isinstance(leaf, str) or (format == "pi-session" and not DIGEST_PATTERN.fullmatch(leaf)):
        raise ValueError("invalid native history leaf digest")
    if format == "codex-session" and leaf:
        raise ValueError("Codex history has no declared leaf")
    for field in ("records", "user_messages", "excluded_user_messages", "excluded_records"):
        if type(value[field]) is not int or not 0 <= value[field] <= MAX_NATIVE_RECORDS:
            raise ValueError("invalid native history adapter coverage count")
    if value["user_messages"] != message_count or value["user_messages"] < 1:
        raise ValueError("native history user coverage disagrees with import")
    if value["records"] != value["user_messages"] + value["excluded_records"]:
        raise ValueError("native history record coverage disagrees")
    if value["excluded_records"] < 1 + value["excluded_user_messages"]:
        raise ValueError("native history excluded coverage disagrees")
