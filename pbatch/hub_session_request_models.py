"""Session-creation schema and transaction helpers shared with the Hub registry."""

import json
import unicodedata

from .hub_store_helpers import HubStoreError, bounded_text, global_session_id


MAX_SESSION_CAPACITY = 8
SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_session_requests(
  request_id TEXT NOT NULL PRIMARY KEY, tenant TEXT NOT NULL,
  actor TEXT NOT NULL, client_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  instance_id TEXT NOT NULL, generation INTEGER NOT NULL, name TEXT NOT NULL,
  project_id TEXT NOT NULL, local_session_id TEXT NOT NULL,
  status TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
  error_code TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
  UNIQUE(tenant,actor,client_id,idempotency_key),
  UNIQUE(tenant,instance_id,local_session_id));
CREATE INDEX IF NOT EXISTS hub_session_requests_pending
  ON hub_session_requests(tenant,instance_id,generation,status,created_at);
"""


def capacity(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SESSION_CAPACITY:
        raise ValueError("session_capacity must be an integer between 0 and 8")
    return value


def session_name(value) -> str:
    if not isinstance(value, str) or any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in value):
        raise ValueError("name must be control-free text")
    result = bounded_text(value.strip(), "name", 256, by_bytes=True)
    if not result:
        raise ValueError("name must not be empty")
    return result


def request_record(row) -> dict:
    return {field: row[field] for field in (
        "request_id", "instance_id", "name", "status", "session_id", "error_code",
        "created_at", "updated_at")}


def command_record(row) -> dict:
    return {field: row[field] for field in (
        "request_id", "instance_id", "generation", "name", "project_id", "local_session_id")}


def request_row(conn, tenant, request_id):
    row = conn.execute("SELECT * FROM hub_session_requests WHERE tenant=? AND request_id=?",
                       (tenant, request_id)).fetchone()
    if row is None:
        raise HubStoreError("not_found", "session request not found", 404)
    return row


def require_capacity(conn, instance) -> None:
    maximum = capacity(json.loads(instance["metadata_json"]).get("session_capacity", 0))
    if maximum == 0:
        raise HubStoreError("session_creation_unsupported", "instance does not support session creation")
    used = session_usage(conn, instance["tenant"], instance["instance_id"], instance["generation"])
    if used >= maximum:
        raise HubStoreError("session_capacity", "instance session capacity is full")


def session_usage(conn, tenant, instance, generation) -> int:
    values = (tenant, instance, generation)
    registered = conn.execute(
        "SELECT COUNT(*) FROM hub_sessions s WHERE tenant=? AND instance_id=? AND generation=? "
        "AND NOT EXISTS (SELECT 1 FROM hub_session_close_requests c WHERE c.tenant=s.tenant "
        "AND c.session_id=s.session_id AND c.generation=s.generation AND c.status='closed')",
        values).fetchone()[0]
    queued = conn.execute(
        "SELECT COUNT(*) FROM hub_session_requests WHERE tenant=? AND instance_id=? "
        "AND generation=? AND status='queued'", values).fetchone()[0]
    return registered + queued


def guard_capacity_update(conn, tenant, instance, generation, maximum) -> None:
    if maximum and session_usage(conn, tenant, instance, generation) > maximum:
        raise HubStoreError("session_capacity", "session capacity is below existing reservations")


def guard_session_registration(conn, instance, local_id, prior) -> None:
    reserved = conn.execute(
        "SELECT status FROM hub_session_requests WHERE tenant=? AND instance_id=? AND local_session_id=?",
        (instance["tenant"], instance["instance_id"], local_id)).fetchone()
    if reserved and reserved["status"] != "created":
        raise HubStoreError("session_reserved", "session identity belongs to a creation request")
    if prior is None and json.loads(instance["metadata_json"]).get("session_capacity", 0):
        require_capacity(conn, instance)


def lose_pending(conn, tenant, instance, now) -> None:
    conn.execute(
        "UPDATE hub_session_requests SET status='lost',error_code='owner_replaced',updated_at=? "
        "WHERE tenant=? AND instance_id=? AND status='queued'", (now, tenant, instance))


def report_result(body) -> tuple[str, str]:
    status, error = body.get("status"), body.get("error_code", "")
    if status == "created" and error == "":
        return status, error
    if status == "failed" and error in ("session_creation_failed", "session_capacity"):
        return status, error
    raise ValueError("invalid session creation result")


def complete_request(conn, row, status, error, now) -> dict:
    if row["status"] != "queued":
        if (row["status"], row["error_code"]) != (status, error):
            raise HubStoreError("session_request_terminal", "session request already has another outcome")
        return request_record(row)
    session_id = ""
    if status == "created":
        session_id = global_session_id(row["tenant"], row["instance_id"], row["local_session_id"])
        conn.execute(
            "INSERT INTO hub_sessions(session_id,tenant,instance_id,local_session_id,generation,"
            "name,project_id,controllable,status,frozen,updated_at) VALUES(?,?,?,?,?,?,?,1,'idle',0,?)",
            (session_id, row["tenant"], row["instance_id"], row["local_session_id"], row["generation"],
             row["name"], row["project_id"], now))
    conn.execute(
        "UPDATE hub_session_requests SET status=?,session_id=?,error_code=?,updated_at=? "
        "WHERE tenant=? AND request_id=?",
        (status, session_id, error, now, row["tenant"], row["request_id"]))
    return request_record(request_row(conn, row["tenant"], row["request_id"]))
