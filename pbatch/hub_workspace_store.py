"""Tenant/session scoped immutable workspace metadata and upload recovery."""

from __future__ import annotations

import json
import uuid

from .hub_compute_store import ComputeStore
from .hub_store_helpers import (HubStoreError, append_internal_event, canonical_json,
                                get_session, identifier)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_workspace_snapshots(
 snapshot_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, actor TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, session_id TEXT NOT NULL, instance_id TEXT NOT NULL,
 project_id TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
 file_count INTEGER NOT NULL, created_at REAL NOT NULL, state TEXT NOT NULL,
 reference_json TEXT NOT NULL, UNIQUE(tenant,actor,idempotency_key));
CREATE INDEX IF NOT EXISTS hub_workspace_page
 ON hub_workspace_snapshots(tenant,session_id,snapshot_id);
"""


def snapshot_record(row):
    value = {key: row[key] for key in ("snapshot_id", "session_id", "instance_id", "project_id",
                                       "sha256", "size", "file_count", "created_at")}
    value["artifact_ref"] = json.loads(row["reference_json"])
    return value


class WorkspaceStore:
    def __init__(self, hub):
        self.hub = hub
        with hub._write() as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)

    def reserve(self, principal, session, key, digest, size, count, *, turn_id=""):
        key = identifier(key, "idempotency_key")
        with self.hub._write() as conn:
            fresh = get_session(conn, principal.tenant_id, session["session_id"])
            if fresh["frozen"] or not fresh["controllable"]:
                raise HubStoreError("session_unavailable", "session cannot upload workspaces")
            ComputeStore._check_delegation(conn, principal, fresh, {"turn_id": turn_id})
            row = conn.execute("SELECT * FROM hub_workspace_snapshots WHERE tenant=? "
                               "AND actor=? AND idempotency_key=?",
                               (principal.tenant_id, principal.subject, key)).fetchone()
            if row:
                if row["session_id"] != session["session_id"] or row["sha256"] != digest:
                    raise HubStoreError("idempotency_conflict", "snapshot key has different content")
                return snapshot_record(row)
            self._capacity(conn, principal)
            snapshot_id = "ws-" + uuid.uuid4().hex
            conn.execute("INSERT INTO hub_workspace_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (snapshot_id, principal.tenant_id, principal.subject, key, fresh["session_id"],
                          fresh["instance_id"], fresh["project_id"], digest, size, count,
                          self.hub.clock(), "pending", "{}"))
            return snapshot_record(self._row(conn, principal.tenant_id, snapshot_id))

    @staticmethod
    def _capacity(conn, principal):
        for predicate, args, ceiling in (
                ("1=1", (), 10000), ("tenant=?", (principal.tenant_id,), 1000),
                ("tenant=? AND actor=?", (principal.tenant_id, principal.subject), 200)):
            count = conn.execute("SELECT COUNT(*) FROM hub_workspace_snapshots WHERE " + predicate,
                                 args).fetchone()[0]
            if count >= ceiling:
                raise HubStoreError("snapshot_quota", "workspace snapshot quota reached", 429)

    @staticmethod
    def _row(conn, tenant, snapshot_id):
        row = conn.execute("SELECT * FROM hub_workspace_snapshots WHERE tenant=? AND snapshot_id=?",
                           (tenant, snapshot_id)).fetchone()
        if row is None:
            raise HubStoreError("not_found", "workspace snapshot not found", 404)
        return row

    def ready(self, tenant, snapshot_id, reference):
        with self.hub._write() as conn:
            row = self._row(conn, tenant, snapshot_id)
            if row["state"] != "ready":
                conn.execute("UPDATE hub_workspace_snapshots SET state='ready',reference_json=? "
                             "WHERE tenant=? AND snapshot_id=?",
                             (canonical_json(reference), tenant, snapshot_id))
                session = get_session(conn, tenant, row["session_id"])
                # Audit owns metadata only; file contents never enter the event ledger.
                context = dict(session, turn_id="")
                append_internal_event(conn, tenant, context, str(uuid.uuid4()), "workspace.ready",
                                      {"snapshot_id": snapshot_id, "sha256": row["sha256"],
                                       "actor": row["actor"]}, self.hub.clock())
            return snapshot_record(self._row(conn, tenant, snapshot_id))

    def get(self, tenant, session_id, snapshot_id):
        conn = self.hub._connect()
        try:
            row = self._row(conn, tenant, snapshot_id)
            if row["session_id"] != session_id:
                raise HubStoreError("not_found", "snapshot not found for this session", 404)
            if row["state"] != "ready":
                raise HubStoreError("snapshot_pending", "workspace upload is not ready")
            return snapshot_record(row)
        finally:
            conn.close()

    def list(self, tenant, session_id, *, after="", limit=100):
        conn = self.hub._connect()
        try:
            rows = conn.execute("SELECT * FROM hub_workspace_snapshots WHERE tenant=? AND session_id=? "
                                "AND state='ready' AND snapshot_id>? ORDER BY snapshot_id LIMIT ?",
                                (tenant, session_id, after, limit))
            return [snapshot_record(row) for row in rows]
        finally:
            conn.close()
