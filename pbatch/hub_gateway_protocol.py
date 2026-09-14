"""Bounded Hub event projection and small wire-protocol helpers."""

from __future__ import annotations

import uuid
from typing import Any

from .memory_records import redact_content
from .tui_protocol import RuntimeEvent

EVENT_NAMESPACE = uuid.UUID("aecdb716-f042-4822-adb2-6fc5d4f91ef7")
VISIBLE_EVENTS = frozenset({
    "turn.started", "turn.completed", "turn.failed", "turn.interrupted",
    "message.delta", "message.completed",
})
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


def project_event_payload(event: RuntimeEvent) -> list[dict[str, Any]]:
    if event.kind in TERMINAL:
        payload = {key: truncate_utf8(event.payload[key], 1024)
                   for key in ("state", "reason")
                   if isinstance(event.payload.get(key), str)}
        return [redact_content(payload)]
    if event.kind not in {"message.delta", "message.completed"}:
        return []
    value = event.payload.get("text", "")
    text = value if isinstance(value, str) else ""
    chunks, truncated = split_utf8(text, EVENT_TEXT_BYTES, MAX_EVENT_TEXT_BYTES)
    count = len(chunks)
    return [redact_content({"text": part, "chunk_index": index,
                            "chunk_count": count, "truncated": truncated})
            for index, part in enumerate(chunks)]


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
