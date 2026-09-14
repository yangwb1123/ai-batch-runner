"""Durable delivery schema and strictly projected lifecycle facts."""

from __future__ import annotations

import json
import uuid

from .hub_ecosystem_http import canonical


SCOPES = frozenset({"agent.integrations:read", "agent.integrations:retry"})
NAMESPACE = uuid.UUID("7e8d12ef-ef8f-425d-ae99-ec1af2787a4d")
KINDS = frozenset(
    "turn." + state for state in (
        "queued", "running", "completed", "failed", "interrupted", "cancel_requested", "cancelled", "lost"))
KINDS |= frozenset("compute." + state for state in (
    "queued", "dispatching", "running", "cancel_requested", "completed", "failed", "cancelled", "lost"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_integration_sinks(
 tenant TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,fingerprint TEXT NOT NULL,
 cursor INTEGER NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(tenant,name));
CREATE TABLE IF NOT EXISTS hub_integration_deliveries(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,delivery_id TEXT NOT NULL UNIQUE,
 tenant TEXT NOT NULL,sink_name TEXT NOT NULL,
 sink_kind TEXT NOT NULL,fingerprint TEXT NOT NULL,event_id TEXT NOT NULL,
 cursor INTEGER NOT NULL,instance_id TEXT NOT NULL,kind TEXT NOT NULL,
 state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,attempt_limit INTEGER NOT NULL DEFAULT 12,
 first_attempt_at REAL,
 retry_at REAL NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
 fact_json TEXT NOT NULL,payload_json TEXT,receipt_json TEXT NOT NULL DEFAULT '{}',
 error_code TEXT NOT NULL DEFAULT '',error_status INTEGER NOT NULL DEFAULT 0,
 attempt_token TEXT NOT NULL DEFAULT '',UNIQUE(tenant,sink_name,event_id));
CREATE INDEX IF NOT EXISTS hub_integration_due
 ON hub_integration_deliveries(tenant,sink_name,state,retry_at);
CREATE INDEX IF NOT EXISTS hub_integration_page
 ON hub_integration_deliveries(tenant,sequence);
"""


def delivery_id(tenant, name, event_id):
    return str(uuid.uuid5(NAMESPACE, canonical([tenant, name, event_id])))


def lifecycle_fact(conn, event):
    """Resolve actors from durable records, never from Gateway-supplied message data."""
    if event["kind"] not in KINDS:
        return None
    payload = json.loads(event["payload_json"])
    task_id = payload.get("task_id", "") if event["kind"].startswith("compute.") else ""
    table, column, key = ("hub_compute_tasks", "task_id", task_id) if task_id else (
        "hub_turns", "turn_id", event["turn_id"])
    row = conn.execute(f"SELECT actor,session_id,instance_id FROM {table} WHERE tenant=? AND {column}=?",
                       (event["tenant"], key)).fetchone()
    if row is None or (row["session_id"], row["instance_id"]) != (
            event["session_id"], event["instance_id"]):
        return None  # An unassociated Gateway lifecycle message is not a state fact.
    trigger = ""
    if event["kind"] in {"turn.cancelled", "turn.cancel_requested"}:
        trigger = payload.get("actor", "")
    elif event["kind"] in {"compute.cancelled", "compute.cancel_requested"}:
        trigger = payload.get("trigger_subject", "")
    return {"tenant_id": event["tenant"], "event_id": event["event_id"],
            "cursor": event["cursor"], "kind": event["kind"], "created_at": event["created_at"],
            "instance_id": event["instance_id"], "session_id": event["session_id"],
            "turn_id": event["turn_id"], "task_id": task_id, "actor_subject": row["actor"],
            "trigger_subject": trigger, "status": event["kind"].split(".", 1)[1]}


def delivery_record(row, *, private=False):
    result = {key: row[key] for key in (
        "sequence", "delivery_id", "sink_name", "sink_kind", "event_id", "cursor", "instance_id", "kind",
        "state", "attempts", "first_attempt_at", "retry_at", "created_at", "updated_at",
        "error_code", "error_status")}
    result["receipt"] = json.loads(row["receipt_json"])
    if private:
        result.update(tenant=row["tenant"], fingerprint=row["fingerprint"],
                      attempt_limit=row["attempt_limit"],
                      attempt_token=row["attempt_token"], fact=json.loads(row["fact_json"]),
                      payload=json.loads(row["payload_json"]) if row["payload_json"] else None)
    return result
