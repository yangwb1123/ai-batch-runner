"""Explicit, bounded native-history conversion; no session discovery or model calls.

Only textual user records are copied. A selected branch/file is not the user's
complete history and its native role/workspace labels are not authenticated.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .task_center_io import checked_path
from .task_goal_schema import text
from .task_intent_schema import fingerprint, validate_export, validate_channel
from .task_history_schema import MAX_NATIVE_BYTES, MAX_NATIVE_LINE_BYTES, MAX_NATIVE_RECORDS, NATIVE_FORMATS


def options(format: str, channel: str, leaf: str) -> None:
    if format == "normalized":
        if channel or leaf:
            raise ValueError("normalized exports carry their own channel; native options are forbidden")
        return
    if format not in NATIVE_FORMATS:
        raise ValueError("unsupported history input format")
    validate_channel(channel)
    if format == "pi-session" and not text(leaf, 128):
        raise ValueError("Pi history needs an explicitly selected --leaf-id")
    if format == "codex-session" and leaf:
        raise ValueError("Codex user-event history does not accept a leaf selection")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate native JSON field")
        value[key] = item
    return value


def records(raw: bytes) -> list:
    if not raw or len(raw) > MAX_NATIVE_BYTES or not raw.endswith(b"\n"):
        raise ValueError("native history exceeds byte budget, is empty, or has an unfinished tail")
    lines = raw.splitlines()
    if len(lines) > MAX_NATIVE_RECORDS:
        raise ValueError("native history record budget exceeded; no records were truncated")
    result = []
    for line in lines:
        if not line or len(line) > MAX_NATIVE_LINE_BYTES:
            raise ValueError("native history has an empty or over-budget line")
        try:
            row = json.loads(line.decode("utf-8"), object_pairs_hook=_object,
                             parse_constant=lambda _: _invalid_constant())
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("invalid native history JSONL") from exc
        if not isinstance(row, dict) or not text(row.get("type"), 80):
            raise ValueError("native history records must be typed objects")
        result.append(row)
    return result


def _invalid_constant():
    raise ValueError("non-finite native JSON value")


def workspace(source: dict, declared) -> str:
    if not source["workspace"] or not isinstance(declared, str) or not Path(declared).is_absolute():
        raise ValueError("native history needs a registered workspace and an absolute declared cwd")
    if checked_path(declared) != checked_path(source["workspace"]):
        raise ValueError("native history declared cwd does not match the registered workspace")
    return fingerprint(str(checked_path(declared)))


def user_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        raise ValueError("native user message has unsupported content")
    parts = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
            raise ValueError("native user multimodal/non-text content is unsupported; no text was silently dropped")
        parts.append(part["text"])
    return "\n".join(parts)


def convert(raw: bytes, source: dict, goal: str, format: str, channel: str, leaf: str) -> tuple[dict, dict]:
    rows = records(raw)
    if format == "pi-session":
        from .task_history_pi import project
    else:
        from .task_history_codex import project
    messages, adapter = project(rows, source, leaf)
    manifest = {"schema_version": 1, "goal_id": goal, "channel": channel,
                "history_complete": False, "messages": messages}
    return validate_export(manifest), adapter


def metadata(format: str, session_id: str, workspace_digest: str, leaf: str,
             rows: list, messages: list, all_users: int) -> dict:
    return {"schema_version": 1, "format": format,
            "session_digest": fingerprint([format, session_id]), "workspace_digest": workspace_digest,
            "selection": "pi_branch_users" if format == "pi-session" else "codex_user_events",
            "leaf_digest": fingerprint(leaf) if leaf else "", "records": len(rows),
            "user_messages": len(messages), "excluded_user_messages": all_users - len(messages),
            "excluded_records": len(rows) - len(messages)}


def check_continuation(value: dict, adapter: dict | None, raw: bytes | None, messages: list) -> None:
    previous = value["imports"][-1].get("adapter") if value["imports"] else None
    if bool(previous) != bool(adapter):
        raise ValueError("history format cannot change within a channel; use a separate channel")
    if adapter is None:
        return
    for record in value["imports"]:
        prior = record["adapter"]
        if any(prior[key] != adapter[key] for key in ("format", "session_digest", "workspace_digest")):
            raise ValueError("native session identity cannot change within a channel")
        proof = record["evidence"]
        if raw is None or hashlib.sha256(raw[:proof["bytes"]]).hexdigest() != proof["sha256"]:
            raise ValueError("native history must preserve its entire previously imported byte prefix")
    retained = [message["id"] for message in value["messages"]]
    # Branch changes may drop old goals: require an explicit separate channel.
    if retained and [message["id"] for message in messages][:len(retained)] != retained:
        raise ValueError("native branch/user history is not a continuation; use a separate channel")
