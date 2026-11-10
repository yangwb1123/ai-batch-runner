"""Bounded Hub event projection and small wire-protocol helpers."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from .memory_records import redact_content
from .tui_protocol import MAX_EVENT_PAYLOAD_BYTES, RuntimeEvent
from .hub_gateway_evidence import EVIDENCE_EVENTS, project_evidence
from .hub_gateway_output import command_output_fields, limit_command_output

EVENT_NAMESPACE = uuid.UUID("aecdb716-f042-4822-adb2-6fc5d4f91ef7")
VISIBLE_EVENTS = frozenset({
    "turn.started", "turn.completed", "turn.failed", "turn.interrupted",
    "message.started", "message.delta", "message.completed",
    "command.started", "command.output", "command.completed", "file.changed",
}) | EVIDENCE_EVENTS
TERMINAL = {"turn.completed": "completed", "turn.failed": "failed",
            "turn.interrupted": "interrupted"}
TERMINAL_STATES = {"completed", "failed", "interrupted", "cancelled", "lost"}
# JSON can expand one control byte into six bytes; leave room for the envelope.
EVENT_TEXT_BYTES = 4 * 1024
MAX_EVENT_TEXT_BYTES = 256 * 1024


def field(value: dict[str, Any], key: str) -> str:
    item = value.get(key, "")
    return str(item).strip() if isinstance(item, (str, int)) else ""


def truth(value: Any) -> bool:
    return value is True or value == 1 or value == "true"


def nested_text(value: Any, key: str) -> str:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, (str, int)) and str(candidate):
            return str(candidate)
        for child in value.values():
            found = nested_text(child, key)
            if found:
                return found
    return ""


def nested_int(value: Any, key: str) -> int:
    try:
        return int(nested_text(value, key))
    except ValueError:
        return 0


def running_event_id(instance_id: str, session_id: str, turn_id: str) -> str:
    identity = f"{instance_id}:{session_id}:{turn_id}:turn.running"
    return str(uuid.uuid5(EVENT_NAMESPACE, identity))


def project_event_payload(event: RuntimeEvent, *,
                          publish_command_output: bool = False) -> list[dict[str, Any]]:
    if type(publish_command_output) is not bool:
        raise ValueError("publish_command_output must be a boolean")
    if event.kind in EVIDENCE_EVENTS:
        return [project_evidence(event, _summary, _item_identity)]
    if event.kind in TERMINAL:
        payload = {key: truncate_utf8(event.payload[key], 1024)
                   for key in ("state", "reason")
                   if isinstance(event.payload.get(key), str)}
        return [redact_content(payload)]
    if event.kind in {"command.started", "command.completed", "file.changed"}:
        fields = _progress_payload(event)
        if event.kind == "command.completed" and publish_command_output:
            fields.update(command_output_fields(event.payload, _summary))
        return [limit_command_output(fields)]
    if event.kind == "command.output":
        fields = command_output_fields(event.payload, _summary) if publish_command_output else {}
        return ([limit_command_output({**_item_identity(event.payload), "text": "",
                 "truncated": event.payload.get("truncated") is True, **fields})] if fields else [])
    if event.kind == "message.started":
        return [_item_identity(event.payload)]
    if event.kind not in {"message.delta", "message.completed"}:
        return []
    value = event.payload.get("text", "")
    text = value if isinstance(value, str) else ""
    bounded = truncate_utf8(text, MAX_EVENT_TEXT_BYTES)
    chunks, cut = split_utf8(redact_content(bounded), EVENT_TEXT_BYTES, MAX_EVENT_TEXT_BYTES)
    truncated = cut or len(bounded) < len(text) or event.payload.get("truncated") is True
    count = len(chunks)
    identity = _item_identity(event.payload)
    return [{**identity, "text": part, "chunk_index": index,
             "chunk_count": count, "truncated": truncated}
            for index, part in enumerate(chunks)]


def _item_identity(payload):
    value = payload.get("item_id", "")
    if isinstance(value, str) and not value:
        return {}
    if not isinstance(value, str) or len(value) > MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError("runtime message item identity exceeds its bound")
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError("runtime message item identity exceeds its bound")
    return {"item_id": "item-" + hashlib.sha256(encoded).hexdigest()}


def _summary(value, maximum):
    raw = value if isinstance(value, str) else ""
    text = redact_content(raw[:65536])
    text = "".join(" " if ord(char) < 32 and char != "\n" or 127 <= ord(char) <= 159
                   or 0x202a <= ord(char) <= 0x202e or 0x2066 <= ord(char) <= 0x2069
                   else char for char in text)
    bounded = truncate_utf8(text, maximum)
    return bounded, len(raw) > 65536 or len(bounded) < len(text)


def _changed_paths(payload):
    values = payload.get("paths", [])
    values = list(values[:17]) if isinstance(values, (list, tuple)) else []
    if isinstance(payload.get("path"), str):
        values.append(payload["path"])
    changes = payload.get("changes", [])
    truncated = len(changes) > 16 if isinstance(changes, list) else bool(changes)
    if isinstance(changes, list):
        values.extend(row.get("path", "") if isinstance(row, dict) else row for row in changes[:17])
    paths = []
    for value in values[:16]:
        if not isinstance(value, str) or not value:
            truncated = True
            continue
        path, cut = _summary(value, 512)
        truncated = truncated or cut
        if path:
            paths.append(path)
    return paths, truncated or len(values) > 16


def _progress_payload(event):
    payload = event.payload
    fields = _item_identity(payload)
    text, truncated = _summary(payload.get("command", ""), 4096)
    if event.kind == "file.changed":
        paths, cut = _changed_paths(payload)
        text, text_cut = _summary("\n".join(paths), 4096)
        fields["paths"] = paths
        truncated = cut or text_cut
    state, cut = _summary(payload.get("status", ""), 128)
    if state:
        fields["state"] = state
    if event.kind == "command.completed":
        code = payload.get("exit_code")
        fields["exit_code"] = code if type(code) is int and -(2 ** 31) <= code < 2 ** 31 else None
    return {**fields, "text": text, "truncated": truncated or cut or payload.get("truncated") is True}


def split_utf8(text: str, chunk_limit: int,
               total_limit: int) -> tuple[list[str], bool]:
    chunks: list[str] = []
    current: list[str] = []
    chunk_bytes = total_bytes = 0
    truncated = False
    for char in text:
        size = len(char.encode("utf-8"))
        if total_bytes + size > total_limit:
            truncated = True
            break
        if current and chunk_bytes + size > chunk_limit:
            chunks.append("".join(current))
            current, chunk_bytes = [], 0
        current.append(char)
        chunk_bytes += size
        total_bytes += size
    if current or not chunks:
        chunks.append("".join(current))
    return chunks, truncated


def truncate_utf8(value: str, byte_limit: int) -> str:
    chars: list[str] = []
    total = 0
    for char in value:
        size = len(char.encode("utf-8"))
        if total + size > byte_limit:
            break
        chars.append(char)
        total += size
    return "".join(chars)


def has_terminal_event(result: Any) -> bool:
    terminal = getattr(result, "terminal_event", None)
    if terminal is not None:
        return str(getattr(terminal, "kind", "")) in TERMINAL
    return isinstance(result, dict) and result.get("state") in TERMINAL.values()


def is_fencing_error(error: Any) -> bool:
    return (getattr(error, "status", 0) == 409
            and getattr(error, "code", "") == "stale_generation")


__all__ = [
    "EVENT_NAMESPACE", "TERMINAL", "TERMINAL_STATES", "VISIBLE_EVENTS",
    "field", "has_terminal_event", "is_fencing_error", "nested_int",
    "nested_text", "project_event_payload", "running_event_id", "truth",
]
