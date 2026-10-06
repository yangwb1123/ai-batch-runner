"""Closed v2 user-range export provenance. Integrity is not authenticated authorship."""
from __future__ import annotations

import re

from .task_center_metrics import timestamp
from .task_goal_schema import text
from .task_history_schema import DIGEST_PATTERN, NATIVE_FORMATS
from .task_history_stream import MAX_SCAN_BYTES, MAX_SCAN_RECORDS

_FIELDS = {"schema_version", "producer", "source_id", "contract_digest", "format", "session_digest",
           "workspace_digest", "leaf_id", "input_evidence", "selection", "messages_sha256", "by", "reason", "at"}
_SELECTION = {"first_user", "last_user", "branch_user_messages", "native_user_messages", "native_records"}
SCOPE_KEYS = ("format", "session_digest", "workspace_digest", "leaf_id")


def validate_origin(value: dict, messages: list) -> None:
    from .task_intent_schema import fields, fingerprint
    fields(value, _FIELDS)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported user-range provenance schema")
    if value["producer"] != "bounded-native-range-v1":
        raise ValueError("unsupported user-range export producer")
    if not isinstance(value["source_id"], str) or not re.fullmatch(r"src-[a-f0-9]{24}", value["source_id"]):
        raise ValueError("invalid user-range source identity")
    for key in ("contract_digest", "session_digest", "workspace_digest", "messages_sha256"):
        if not isinstance(value[key], str) or not DIGEST_PATTERN.fullmatch(value[key]):
            raise ValueError("invalid user-range provenance digest")
    if value["messages_sha256"] != fingerprint(messages) or any(m["role"] != "user" for m in messages):
        raise ValueError("user-range messages disagree with export provenance")
    _native_shape(value)
    _selection(value["selection"], len(messages))
    if not text(value["by"], 128) or not text(value["reason"], 1024) or timestamp(value["at"]) is None:
        raise ValueError("invalid user-range export audit actor/time")


def _native_shape(value: dict) -> None:
    from .task_intent_schema import fields
    from .task_native_history import options
    if not isinstance(value["format"], str) or value["format"] not in NATIVE_FORMATS:
        raise ValueError("user-range export needs native input provenance")
    if not isinstance(value["leaf_id"], str):
        raise ValueError("user-range native leaf must be an explicit string")
    options(value["format"], "export_provenance", value["leaf_id"])
    proof = value["input_evidence"]
    fields(proof, {"path", "bytes", "sha256"})
    if not text(proof["path"], 4096) or type(proof["bytes"]) is not int or not 0 < proof["bytes"] <= MAX_SCAN_BYTES:
        raise ValueError("invalid user-range native evidence size/path")
    if not isinstance(proof["sha256"], str) or not DIGEST_PATTERN.fullmatch(proof["sha256"]):
        raise ValueError("invalid user-range native evidence digest")


def _selection(value: dict, count: int) -> None:
    from .task_intent_schema import fields
    fields(value, _SELECTION)
    if any(type(n) is not int or not 1 <= n <= MAX_SCAN_RECORDS for n in value.values()):
        raise ValueError("invalid user-range selection counts")
    first, last = value["first_user"], value["last_user"]
    if not 1 <= first <= last <= value["branch_user_messages"] <= value["native_user_messages"] < value["native_records"]:
        raise ValueError("inconsistent user-range native coverage")
    if last - first + 1 != count:
        raise ValueError("user-range message count disagrees with explicit ordinals")


def validate_record(record: dict, messages: list) -> None:
    if timestamp(record["origin_replayed_at"]) is None:
        raise ValueError("user-range import lacks replay audit time")
    known = {m["id"]: m for m in messages}
    selected = [known[key] for key in record["message_ids"]]
    validate_origin(record["export_origin"], selected)


def validate_chain(value: dict) -> None:
    previous = None
    mode = "export_origin" in value["imports"][0]
    for record in value["imports"]:
        if ("export_origin" in record) != mode:
            raise ValueError("user-range and other history formats cannot share a channel")
        if not mode:
            continue
        current = record["export_origin"]
        if current["source_id"] != value["source_id"]:
            raise ValueError("user-range history source changed")
        if previous is not None:
            check_scope(previous, current)
        previous = current
    if mode and value["history_complete"]:
        raise ValueError("selected user ranges cannot claim complete history")


def check_scope(previous: dict, current: dict) -> None:
    if any(previous[key] != current[key] for key in SCOPE_KEYS):
        raise ValueError("user-range session/workspace/leaf cannot change inside a channel")
    old, new = previous["selection"], current["selection"]
    if new["first_user"] > old["last_user"] + 1 or new["last_user"] < old["last_user"]:
        raise ValueError("user-range continuation has a gap or shrinks; use a separate channel")
