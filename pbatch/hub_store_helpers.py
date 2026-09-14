"""Validation, SQLite schema, and response shaping for HubStore."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid


MAX_PAGE = 200
MAX_EVENT_PAGE = 200
MAX_PROMPT_BYTES = 16 * 1024
MAX_EVENT_BYTES = 32 * 1024
MAX_METADATA_BYTES = 16 * 1024
_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|private.?key|credential|api.?key)", re.I)
_SESSION_NAMESPACE = uuid.UUID("5a9cb2e9-3770-477a-bcb0-0b4184643f63")
_TERMINAL = {"completed", "failed", "interrupted", "cancelled", "lost"}
_TURN_EVENT_STATES = {
    "turn.running": "running", "turn.completed": "completed",
    "turn.failed": "failed", "turn.interrupted": "interrupted",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_instances(
  tenant TEXT NOT NULL, instance_id TEXT NOT NULL, boot_id TEXT NOT NULL,
  generation INTEGER NOT NULL, last_seen REAL NOT NULL, metadata_json TEXT NOT NULL,
  PRIMARY KEY(tenant,instance_id));
CREATE TABLE IF NOT EXISTS hub_sessions(
  session_id TEXT NOT NULL PRIMARY KEY, tenant TEXT NOT NULL, instance_id TEXT NOT NULL,
  local_session_id TEXT NOT NULL, generation INTEGER NOT NULL, name TEXT NOT NULL,
  project_id TEXT NOT NULL, controllable INTEGER NOT NULL, status TEXT NOT NULL,
  frozen INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
  UNIQUE(tenant,instance_id,local_session_id));
CREATE INDEX IF NOT EXISTS hub_sessions_tenant_id ON hub_sessions(tenant,session_id);
CREATE TABLE IF NOT EXISTS hub_turns(
  turn_id TEXT NOT NULL PRIMARY KEY, tenant TEXT NOT NULL, session_id TEXT NOT NULL,
  instance_id TEXT NOT NULL, generation INTEGER NOT NULL, actor TEXT NOT NULL,
  prompt TEXT NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  UNIQUE(tenant,actor,idempotency_key));
CREATE INDEX IF NOT EXISTS hub_turns_active ON hub_turns(tenant,session_id,status);
CREATE TABLE IF NOT EXISTS hub_commands(
  command_id TEXT NOT NULL PRIMARY KEY, tenant TEXT NOT NULL, instance_id TEXT NOT NULL,
  generation INTEGER NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
  kind TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
  UNIQUE(tenant,turn_id,kind));
CREATE INDEX IF NOT EXISTS hub_commands_pending
  ON hub_commands(tenant,instance_id,generation,status,created_at);
CREATE TABLE IF NOT EXISTS hub_cursors(tenant TEXT NOT NULL PRIMARY KEY,next_cursor INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS hub_events(
  tenant TEXT NOT NULL, cursor INTEGER NOT NULL, event_id TEXT NOT NULL,
  instance_id TEXT NOT NULL, generation INTEGER NOT NULL, session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  canonical_json TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(tenant,cursor), UNIQUE(tenant,event_id));
CREATE INDEX IF NOT EXISTS hub_events_session_cursor
  ON hub_events(tenant,session_id,cursor);
"""


class HubStoreError(RuntimeError):
    """An expected Hub persistence or state-transition failure."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def identifier(value, label: str, maximum: int = 256) -> str:
    text = bounded_text(value, label, maximum)
    if not text or text != text.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError(f"{label} is invalid")
    return text


def bounded_text(value, label: str, maximum: int, *, by_bytes: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    size = len(value.encode("utf-8")) if by_bytes else len(value)
    if size > maximum:
        raise ValueError(f"{label} exceeds {maximum} limit")
    return value


def boolean(value, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean")
    return value


def page_limit(value, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def nonnegative_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def allowed_instance_ids(values) -> tuple[str, ...] | None:
    if values is None:
        return None
    if isinstance(values, str):
        raise ValueError("allowed_instances must be a sequence")
    result = tuple(sorted({identifier(value, "allowed_instance") for value in values}))
    return result


def safe_object(value, maximum: int, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if has_secret_key(value):
        raise ValueError(f"{label} contains a sensitive key")
    encoded = canonical_json(value)
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes")
    return value


def has_secret_key(value) -> bool:
    if isinstance(value, dict):
        return any(_SECRET_KEY.search(str(key)) or has_secret_key(item)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_secret_key(item) for item in value)
    return False


def canonical_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be finite JSON data") from exc


def global_session_id(tenant: str, instance: str, local_id: str) -> str:
    return str(uuid.uuid5(_SESSION_NAMESPACE, "\0".join((tenant, instance, local_id))))


def instance_record(row: sqlite3.Row, now: float, offline_after: float) -> dict:
    record = {"tenant": row["tenant"], "instance_id": row["instance_id"],
              "boot_id": row["boot_id"], "generation": int(row["generation"]),
              "last_seen": float(row["last_seen"]),
              "online": now - float(row["last_seen"]) <= offline_after}
    record.update(json.loads(row["metadata_json"]))
    return record


def session_record(conn: sqlite3.Connection, row: sqlite3.Row,
                   now: float, offline_after: float) -> dict:
    instance = conn.execute(
        "SELECT generation,last_seen FROM hub_instances WHERE tenant=? AND instance_id=?",
        (row["tenant"], row["instance_id"])).fetchone()
    online = bool(instance and int(instance["generation"]) == int(row["generation"])
                  and now - float(instance["last_seen"]) <= offline_after)
    active = conn.execute(
        "SELECT turn_id,status FROM hub_turns WHERE tenant=? AND session_id=? "
        "AND status IN ('queued','running','cancel_requested') LIMIT 1",
        (row["tenant"], row["session_id"])).fetchone()
    return {"session_id": row["session_id"], "tenant": row["tenant"],
            "instance_id": row["instance_id"],
            "local_session_id": row["local_session_id"],
            "generation": int(row["generation"]), "name": row["name"],
            "project_id": row["project_id"], "status": active["status"] if active else row["status"],
            "active_turn_id": active["turn_id"] if active else "",
            "online": online,
            "controllable": bool(row["controllable"]) and not bool(row["frozen"])
            and online, "frozen": bool(row["frozen"]),
            "updated_at": float(row["updated_at"])}


def turn_record(row: sqlite3.Row) -> dict:
    return {"turn_id": row["turn_id"], "tenant": row["tenant"],
            "session_id": row["session_id"], "instance_id": row["instance_id"],
            "generation": int(row["generation"]), "actor": row["actor"],
            "state": row["status"], "status": row["status"], "idempotency_key": row["idempotency_key"],
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"])}


def command_record(row: sqlite3.Row) -> dict:
    return {"command_id": row["command_id"], "kind": row["kind"],
            "turn_id": row["turn_id"], "session_id": row["session_id"],
            "local_session_id": row["local_session_id"],
            "prompt": row["prompt"] if row["kind"] == "prompt" else "",
            "state": row["turn_state"], "cancel_requested": row["kind"] == "cancel"}


def append_queued_event(conn: sqlite3.Connection, tenant: str, row: sqlite3.Row, now: float):
    from .memory_records import redact_content
    event_id = str(uuid.uuid5(_SESSION_NAMESPACE, "queued:" + row["turn_id"]))
    return append_internal_event(conn, tenant, row, event_id, "turn.queued",
                                 {"text": redact_content(row["prompt"]), "actor": row["actor"]}, now)


def event_record(row: sqlite3.Row, duplicate: bool = False) -> dict:
    return {"cursor": int(row["cursor"]), "event_id": row["event_id"],
            "session_id": row["session_id"], "turn_id": row["turn_id"],
            "instance_id": row["instance_id"], "generation": int(row["generation"]),
            "kind": row["kind"], "payload": json.loads(row["payload_json"]),
            "created_at": float(row["created_at"]), "duplicate": duplicate}


def append_event_tx(conn: sqlite3.Connection, tenant: str, instance: str,
                    generation: int, event_id: str, session_id: str,
                    turn_id: str, kind: str, payload: dict,
                    canonical: str, now: float) -> dict:
    row = conn.execute(
        "SELECT next_cursor FROM hub_cursors WHERE tenant=?", (tenant,)).fetchone()
    cursor = int(row["next_cursor"]) if row else 1
    conn.execute(
        "INSERT INTO hub_cursors(tenant,next_cursor) VALUES(?,?) "
        "ON CONFLICT(tenant) DO UPDATE SET next_cursor=excluded.next_cursor",
        (tenant, cursor + 1))
    conn.execute(
        "INSERT INTO hub_events(tenant,cursor,event_id,instance_id,generation,session_id,"
        "turn_id,kind,payload_json,canonical_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (tenant, cursor, event_id, instance, generation, session_id, turn_id,
         kind, canonical_json(payload), canonical, now))
    stored = conn.execute(
        "SELECT * FROM hub_events WHERE tenant=? AND event_id=?",
        (tenant, event_id)).fetchone()
    return event_record(stored)


def append_internal_event(conn: sqlite3.Connection, tenant: str,
                          turn: sqlite3.Row, event_id: str, kind: str,
                          payload: dict, now: float) -> dict:
    canonical = canonical_json({"session_id": turn["session_id"],
                                "turn_id": turn["turn_id"], "kind": kind,
                                "payload": payload})
    return append_event_tx(
        conn, tenant, turn["instance_id"], turn["generation"], event_id,
        turn["session_id"], turn["turn_id"], kind, payload, canonical, now)


def require_generation(conn: sqlite3.Connection, tenant: str,
                       instance: str, generation: int) -> sqlite3.Row:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ValueError("generation must be a positive integer")
    row = conn.execute(
        "SELECT * FROM hub_instances WHERE tenant=? AND instance_id=?",
        (tenant, instance)).fetchone()
    if row is None:
        raise HubStoreError("not_found", "instance not found", 404)
    if int(row["generation"]) != generation:
        raise HubStoreError("stale_generation", "instance generation is stale")
    return row


def require_live_generation(conn: sqlite3.Connection, tenant: str,
                            instance: str, generation: int, now: float,
                            offline_after: float) -> sqlite3.Row:
    row = require_generation(conn, tenant, instance, generation)
    if now - float(row["last_seen"]) > offline_after:
        raise HubStoreError("instance_offline", "instance heartbeat has expired")
    return row


def get_session(conn: sqlite3.Connection, tenant: str,
                session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM hub_sessions WHERE tenant=? AND session_id=?",
        (tenant, session_id)).fetchone()
    if row is None:
        raise HubStoreError("not_found", "session not found", 404)
    return row


def check_session_owner(row: sqlite3.Row, instance: str,
                        generation: int) -> None:
    if row["instance_id"] != instance or int(row["generation"]) != generation or row["frozen"]:
        raise HubStoreError("stale_generation", "session owner generation is stale")


def get_turn_for_session(conn: sqlite3.Connection, tenant: str,
                         turn_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM hub_turns WHERE tenant=? AND turn_id=? AND session_id=?",
        (tenant, turn_id, session_id)).fetchone()
    if row is None:
        raise HubStoreError("not_found", "turn not found for session", 404)
    return row


def ensure_no_active_turn(conn: sqlite3.Connection, tenant: str,
                          session_id: str) -> None:
    row = conn.execute(
        "SELECT turn_id FROM hub_turns WHERE tenant=? AND session_id=? "
        "AND status IN ('queued','running','cancel_requested') LIMIT 1",
        (tenant, session_id)).fetchone()
    if row:
        raise HubStoreError("turn_active", "session already has a non-terminal turn")


def apply_turn_event(conn: sqlite3.Connection, tenant: str,
                     turn_id: str, kind: str, now: float) -> None:
    row = conn.execute(
        "SELECT status FROM hub_turns WHERE tenant=? AND turn_id=?",
        (tenant, turn_id)).fetchone()
    if row is None:
        raise HubStoreError("not_found", "turn not found", 404)
    allowed = {"turn.running": {"queued"},
               "turn.completed": {"running", "cancel_requested"},
               "turn.failed": {"queued", "running", "cancel_requested"},
               "turn.interrupted": {"running", "cancel_requested"}}
    current = row["status"]
    if current in _TERMINAL:
        raise HubStoreError("turn_terminal", "turn is already terminal")
    if current not in allowed[kind]:
        raise HubStoreError("turn_state", f"cannot transition {current} via {kind}")
    target = _TURN_EVENT_STATES[kind]
    conn.execute(
        "UPDATE hub_turns SET status=?,updated_at=? WHERE tenant=? AND turn_id=?",
        (target, now, tenant, turn_id))
    if target == "running":
        conn.execute(
            "UPDATE hub_commands SET status='resolved' WHERE tenant=? AND turn_id=? AND kind='prompt'",
            (tenant, turn_id))
    elif target in _TERMINAL:
        conn.execute(
            "UPDATE hub_commands SET status='resolved' WHERE tenant=? AND turn_id=?",
            (tenant, turn_id))
