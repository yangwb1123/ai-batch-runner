"""Codex rollout JSONL: canonical user_message events, not response-item mirrors."""
from __future__ import annotations

from .task_goal_schema import text
from .task_intent_schema import fingerprint
from .task_native_history import metadata, workspace


def _message(payload: dict) -> str:
    if set(payload) - {"type", "message", "role", "images", "local_images", "text_elements", "attachments"}:
        raise ValueError("unsupported Codex user-event fields; no input was silently dropped")
    if any(payload.get(key) for key in ("images", "local_images", "text_elements", "attachments")):
        raise ValueError("Codex user attachments/annotated content are unsupported; no input was silently dropped")
    if "role" in payload and payload["role"] != "user":
        raise ValueError("conflicting Codex user-event role")
    value = payload.get("message")
    if not isinstance(value, str):
        raise ValueError("Codex user event requires textual message content")
    return value


def project(rows: list, source: dict, leaf: str) -> tuple[list, dict]:
    header = rows[0]
    data = header.get("payload")
    if header.get("type") != "session_meta" or not isinstance(data, dict) or not text(data.get("id"), 128):
        raise ValueError("Codex history needs a session_meta identity header")
    identity = data["id"]
    root = workspace(source, data.get("cwd"))
    messages = []
    for ordinal, row in enumerate(rows[1:], 1):
        if row["type"] == "session_meta":
            raise ValueError("multiple Codex native sessions cannot share an import")
        payload = row.get("payload")
        if row["type"] != "event_msg":
            continue  # response_item user mirrors are not additional authored prompts
        if not isinstance(payload, dict) or not text(payload.get("type"), 80):
            raise ValueError("Codex event message type is missing")
        if payload["type"] != "user_message":
            continue
        messages.append({"id": "codex-" + fingerprint([identity, ordinal]),
                         "ts": row.get("timestamp"), "role": "user", "text": _message(payload)})
    return messages, metadata("codex-session", identity, root, "", rows, messages, len(messages))
