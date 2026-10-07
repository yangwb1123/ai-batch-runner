"""Immutable single-file preservation receipts; not complete-archive or disposal grants."""
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
    return "pp-" + digest({key: value[key] for key in
        ("resource_id", "resource_version", "source_id", "source_digest", "source_stat", "payload")})[:32]


def validate_receipt(value: dict) -> None:
    _fields(value, _FIELDS)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported preservation receipt schema")
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
    if not stat.S_ISREG(value["source_stat"]["mode"]):
        raise ValueError("preservation receipts support regular file snapshots only")
    _fields(value["payload"], {"bytes", "sha256"})
    _integer(value["payload"]["bytes"])
    if value["payload"]["bytes"] > MAX_PAYLOAD_BYTES or value["payload"]["bytes"] != value["source_stat"]["bytes"]:
        raise ValueError("preservation payload byte bound/source count mismatch")
