"""Bounded prompt-history grants; exported roles are claims, not authenticated users."""
from __future__ import annotations

import hashlib
import json
import re

from .task_center_metrics import timestamp
from .task_goal_schema import GOAL_PATTERN, text
from .task_history_schema import MAX_NATIVE_BYTES, validate_adapter

INTENT_PATTERN = re.compile(r"ih-[a-f0-9]{32}\Z")
MAX_MESSAGES = 128
MAX_TEXT_BYTES = 64 * 1024
MAX_IMPORTS = 32
ROLES = {"user", "assistant", "system", "tool"}


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def intent_key(source_id: str, goal_id: str, channel: str) -> str:
    return "ih-" + fingerprint([source_id, goal_id, channel])[:32]


def fields(value: dict, expected: set) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid prompt history fields")


def validate_export(value: dict) -> dict:
    fields(value, {"schema_version", "goal_id", "channel", "history_complete", "messages"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported prompt history schema")
    if not isinstance(value["goal_id"], str) or not GOAL_PATTERN.fullmatch(value["goal_id"]):
        raise ValueError("invalid history goal identity")
    validate_channel(value["channel"])
    if type(value["history_complete"]) is not bool:
        raise ValueError("history coverage must be an explicit boolean")
    validate_messages(value["messages"])
    return value


def validate_channel(channel: str) -> None:
    if not text(channel, 80) or not re.fullmatch(r"[a-zA-Z0-9_-]+", channel):
        raise ValueError("invalid history channel")


def validate_messages(messages: list) -> None:
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("prompt history message budget exceeded or empty")
    seen, previous, total = set(), None, 0
    for message in messages:
        fields(message, {"id", "ts", "role", "text"})
        if not text(message["id"], 128) or message["id"] in seen:
            raise ValueError("invalid or duplicate history message identity")
        if not isinstance(message["role"], str) or message["role"] not in ROLES:
            raise ValueError("unsupported history message role")
        time = timestamp(message["ts"])
        if not text(message["ts"], 64) or time is None or (previous is not None and time < previous):
            raise ValueError("history timestamps must be valid and chronological")
        if not text(message["text"], 16 * 1024):
            raise ValueError("history message must have bounded complete text")
        total += len(message["text"].encode("utf-8"))
        if total > MAX_TEXT_BYTES:
            raise ValueError("prompt history text byte budget exceeded; no text was truncated")
        seen.add(message["id"])
        previous = time


def validate_state(value: dict) -> dict:
    fields(value, {"schema_version", "intent_id", "source_id", "goal_id", "channel", "version",
        "contract_digest", "authorized", "history_complete", "messages", "messages_digest", "imports", "audit"})
    export = {key: value[key] for key in
              ("schema_version", "goal_id", "channel", "history_complete", "messages")}
    validate_export(export)
    if value["messages_digest"] != fingerprint(value["messages"]):
        raise ValueError("history message digest mismatch")
    if not isinstance(value["source_id"], str) or not re.fullmatch(r"src-[a-f0-9]{24}", value["source_id"]):
        raise ValueError("invalid history source")
    if value["intent_id"] != intent_key(value["source_id"], value["goal_id"], value["channel"]):
        raise ValueError("history identity/scope mismatch")
    if type(value["version"]) is not int or value["version"] < 1 or type(value["authorized"]) is not bool:
        raise ValueError("invalid history revision/authorization")
    if not isinstance(value["contract_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["contract_digest"]):
        raise ValueError("invalid history contract binding")
    _validate_records(value)
    return value


def _validate_records(value: dict) -> None:
    imports, audit = value["imports"], value["audit"]
    if not isinstance(imports, list) or not 1 <= len(imports) <= MAX_IMPORTS:
        raise ValueError("history import budget exceeded")
    if not isinstance(audit, list) or len(audit) != value["version"] or len(audit) > MAX_IMPORTS:
        raise ValueError("history audit budget exceeded or incomplete")
    _validate_audit(audit)
    added = []
    for record in imports:
        _validate_import(record, value["messages"])
        added.extend(record["new_message_ids"])
    if added != [message["id"] for message in value["messages"]]:
        raise ValueError("history messages lack chronological import provenance")
    if sum(record["action"] == "imported" for record in audit) != len(imports):
        raise ValueError("history import/audit coverage mismatch")
    if value["authorized"] != (audit[-1]["action"] == "imported"):
        raise ValueError("history consent/audit mismatch")
    _validate_native_chain(value)


def _validate_native_chain(value: dict) -> None:
    first = value["imports"][0].get("adapter")
    retained = [message["id"] for message in value["messages"]]
    for record in value["imports"]:
        adapter = record.get("adapter")
        if bool(first) != bool(adapter):
            raise ValueError("history format changed inside a channel")
        if adapter is None:
            continue
        if any(adapter[key] != first[key] for key in ("format", "session_digest", "workspace_digest")):
            raise ValueError("native history identity changed inside a channel")
        if record["message_ids"] != retained[:len(record["message_ids"])]:
            raise ValueError("native history is not a chronological branch continuation")
    if first and (value["history_complete"] or any(message["role"] != "user" for message in value["messages"])):
        raise ValueError("native user projection cannot claim complete history or other roles")


def _validate_audit(audit: list) -> None:
    for index, record in enumerate(audit, 1):
        fields(record, {"version", "action", "by", "reason", "at"})
        if type(record["version"]) is not int or record["version"] != index:
            raise ValueError("invalid history audit revision")
        if record["action"] not in {"imported", "revoked"}:
            raise ValueError("invalid history audit action")
        if not text(record["by"], 128) or not text(record["reason"], 1024) or timestamp(record["at"]) is None:
            raise ValueError("invalid history audit actor/time")


def _validate_import(record: dict, messages: list) -> None:
    expected = {"evidence", "message_ids", "new_message_ids", "at", "analysis_seconds"}
    fields(record, expected | ({"adapter"} if isinstance(record, dict) and "adapter" in record else set()))
    _validate_evidence(record)
    known = {message["id"] for message in messages}
    for key in ("message_ids", "new_message_ids"):
        ids = record[key]
        if not isinstance(ids, list) or len(ids) > MAX_MESSAGES or not all(isinstance(item, str) for item in ids):
            raise ValueError("invalid import message references")
        if len(set(ids)) != len(ids) or not set(ids) <= known:
            raise ValueError("history citation points outside its own channel")
    if not set(record["new_message_ids"]) <= set(record["message_ids"]):
        raise ValueError("invalid incremental history coverage")
    if "adapter" in record:
        validate_adapter(record["adapter"], len(record["message_ids"]))
    from .task_center_metrics import number
    if number(record["analysis_seconds"]) is None or timestamp(record["at"]) is None:
        raise ValueError("invalid history analysis accounting")


def _validate_evidence(record: dict) -> None:
    evidence = record["evidence"]
    fields(evidence, {"path", "bytes", "sha256"})
    maximum = MAX_NATIVE_BYTES if "adapter" in record else 128 * 1024
    if not text(evidence["path"], 4096) or type(evidence["bytes"]) is not int or not 0 < evidence["bytes"] <= maximum:
        raise ValueError("invalid history evidence size/path")
    if not isinstance(evidence["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", evidence["sha256"]):
        raise ValueError("invalid history evidence digest")
