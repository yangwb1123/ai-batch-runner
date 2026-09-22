"""Hub-owned close lifecycle: frozen admission, verified release and history."""

import json

from .hub_store_helpers import HubStoreError, require_live_generation


SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_session_close_requests(
  request_id TEXT NOT NULL PRIMARY KEY, tenant TEXT NOT NULL,
  actor TEXT NOT NULL, client_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  instance_id TEXT NOT NULL, generation INTEGER NOT NULL,
  session_id TEXT NOT NULL, local_session_id TEXT NOT NULL, status TEXT NOT NULL,
  error_code TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
  UNIQUE(tenant,actor,client_id,idempotency_key), UNIQUE(tenant,session_id));
CREATE INDEX IF NOT EXISTS hub_session_close_requests_pending
  ON hub_session_close_requests(tenant,instance_id,generation,status,created_at);
"""


def request_record(row) -> dict:
    return {field: row[field] for field in (
        "request_id", "instance_id", "session_id", "status", "error_code", "created_at", "updated_at")}


def command_record(row) -> dict:
    return {field: row[field] for field in (
        "request_id", "instance_id", "generation", "session_id", "local_session_id")}


def request_row(conn, tenant, request_id):
    row = conn.execute("SELECT * FROM hub_session_close_requests WHERE tenant=? AND request_id=?",
                       (tenant, request_id)).fetchone()
    if row is None:
        raise HubStoreError("not_found", "session close request not found", 404)
    return row


def require_available(conn, session, now, offline_after):
    tenant, session_id = session["tenant"], session["session_id"]
    prior = conn.execute("SELECT status FROM hub_session_close_requests WHERE tenant=? AND session_id=?",
                         (tenant, session_id)).fetchone()
    if prior:
        code = {"queued": "session_close_in_progress", "closed": "session_closed",
                "failed": "session_close_failed"}.get(prior["status"], "session_unavailable")
        raise HubStoreError(code, "session already has a close request")
    inst = conn.execute("SELECT * FROM hub_instances WHERE tenant=? AND instance_id=?",
                        (tenant, session["instance_id"])).fetchone()
    if inst is None:
        raise HubStoreError("not_found", "instance not found", 404)
    require_live_generation(conn, tenant, inst["instance_id"], inst["generation"], now, offline_after)
    if session["generation"] != inst["generation"] or session["frozen"] or not session["controllable"]:
        raise HubStoreError("session_unavailable", "session cannot be closed")
    if json.loads(inst["metadata_json"]).get("session_close_supported", False) is not True:
        raise HubStoreError("session_close_unsupported", "instance does not support session close")
    require_idle(conn, tenant, session_id)


def require_idle(conn, tenant, session_id):
    turn = conn.execute(
        "SELECT 1 FROM hub_turns WHERE tenant=? AND session_id=? "
        "AND status IN ('queued','running','cancel_requested') LIMIT 1", (tenant, session_id)).fetchone()
    if turn:
        raise HubStoreError("session_busy", "session has active work")
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_compute_tasks'")
    if table.fetchone() is None:
        return
    task = conn.execute(
        "SELECT 1 FROM hub_compute_tasks WHERE tenant=? AND session_id=? "
        "AND state NOT IN ('completed','failed','cancelled','lost') LIMIT 1",
        (tenant, session_id)).fetchone()
    if task:
        raise HubStoreError("session_busy", "session has active work")


def report_result(body) -> tuple[str, str]:
    status, error = body.get("status"), body.get("error_code", "")
    if status == "closed" and error == "":
        return status, error
    if status == "failed" and error in ("session_close_failed", "session_unavailable"):
        return status, error
    raise ValueError("invalid session close result")


def complete_request(conn, row, status, error, now):
    if row["status"] != "queued":
        if (row["status"], row["error_code"]) != (status, error):
            raise HubStoreError("session_request_terminal", "session close already has another outcome")
        return request_record(row)
    conn.execute(
        "UPDATE hub_sessions SET status=?,updated_at=? WHERE tenant=? AND session_id=? AND generation=?",
        ("closed" if status == "closed" else "close_failed", now,
         row["tenant"], row["session_id"], row["generation"]))
    conn.execute(
        "UPDATE hub_session_close_requests SET status=?,error_code=?,updated_at=? "
        "WHERE tenant=? AND request_id=?", (status, error, now, row["tenant"], row["request_id"]))
    return request_record(request_row(conn, row["tenant"], row["request_id"]))


def lose_pending(conn, tenant, instance, now):
    conn.execute(
        "UPDATE hub_sessions SET status='close_lost',updated_at=? WHERE tenant=? AND session_id IN "
        "(SELECT session_id FROM hub_session_close_requests WHERE tenant=? AND instance_id=? "
        "AND status='queued')", (now, tenant, tenant, instance))
    conn.execute(
        "UPDATE hub_session_close_requests SET status='lost',error_code='owner_replaced',updated_at=? "
        "WHERE tenant=? AND instance_id=? AND status='queued'", (now, tenant, instance))
