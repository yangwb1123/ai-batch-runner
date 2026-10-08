"""Immutable file/directory-byte receipts; not complete recovery or disposal grants."""
from __future__ import annotations

import re
import stat

from .task_goal_schema import text
from .task_retention_schema import (RESOURCE_PATTERN, SOURCE_PATTERN, _fields, _integer,
                                   digest, instant)

PRESERVATION_PATTERN = re.compile(r"pp-[a-f0-9]{32}\Z")
MAX_RECEIPTS = 64
MAX_RECEIPT_BYTES = 8192
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_FIELDS = {"schema_version", "preservation_id", "resource_id", "resource_version", "source_id",
           "source_digest", "source_stat", "payload", "created_at", "by", "reason"}
_STAT = {"device", "inode", "uid", "mode", "bytes", "mtime_ns", "ctime_ns"}


def receipt_key(value: dict) -> str:
    fields = ("resource_id", "resource_version", "source_id", "source_digest", "source_stat", "payload")
    if value.get("schema_version") == 2:
        fields += ("directory_copy",)
    return "pp-" + digest({key: value[key] for key in fields})[:32]


def validate_receipt(value: dict) -> None:
    version = value.get("schema_version") if isinstance(value, dict) else None
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("unsupported preservation receipt schema")
    _fields(value, _FIELDS | ({"directory_copy"} if version == 2 else set()))
    for key, pattern in (("resource_id", RESOURCE_PATTERN), ("source_id", SOURCE_PATTERN)):
        if not isinstance(value[key], str) or not pattern.fullmatch(value[key]):
            raise ValueError("invalid preservation resource/source identity")
    _integer(value["resource_version"], 1)
    if value["resource_version"] > 16:
        raise ValueError("preservation resource revision exceeds schema capacity")
    _payload(value)
    for item in (value["source_digest"], value["payload"]["sha256"]):
        if not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item):
            raise ValueError("invalid preservation digest")
    if value["preservation_id"] != receipt_key(value):
        raise ValueError("preservation receipt identity mismatch")
    instant(value["created_at"])
    if not text(value["by"], 128) or not text(value["reason"], 256):
        raise ValueError("preservation receipt needs unauthenticated actor label and reason")


def _payload(value: dict) -> None:
    _fields(value["source_stat"], _STAT)
    for number in value["source_stat"].values():
        _integer(number)
    file = value["schema_version"] == 1
    matches = stat.S_ISREG(value["source_stat"]["mode"]) if file else stat.S_ISDIR(value["source_stat"]["mode"])
    if not matches:
        raise ValueError("preservation receipt source type conflicts with its schema")
    _fields(value["payload"], {"bytes", "sha256"})
    _integer(value["payload"]["bytes"])
    if value["payload"]["bytes"] > MAX_PAYLOAD_BYTES or (file and value["payload"]["bytes"] != value["source_stat"]["bytes"]):
        raise ValueError("preservation payload byte bound/source count mismatch")
    if not file:
        from .task_directory_schema import validate_summary
        validate_summary(value["directory_copy"], value["payload"]["bytes"])
