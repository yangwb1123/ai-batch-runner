"""Durable idle close requests fenced by tenant, subject and Gateway generation."""

import uuid

from .hub_session_close_models import (
    command_record, complete_request, report_result, request_record, request_row, require_available,
)
from .hub_store_helpers import HubStoreError, get_session, identifier, require_live_generation


class SessionCloseStore:
    def __init__(self, hub):
        self.hub = hub

    def close(self, tenant, session_id, actor, key, *, client_id):
        tenant, session_id = identifier(tenant, "tenant"), identifier(session_id, "session_id")
        actor, client_id = identifier(actor, "actor"), identifier(client_id, "client_id")
        key = identifier(key, "idempotency_key")
        with self.hub._write() as conn:
            prior = conn.execute(
                "SELECT * FROM hub_session_close_requests WHERE tenant=? AND actor=? "
                "AND client_id=? AND idempotency_key=?", (tenant, actor, client_id, key)).fetchone()
            if prior:
                if prior["session_id"] != session_id:
                    raise HubStoreError("idempotency_conflict", "session close key has different content")
                return request_record(prior)
            session = get_session(conn, tenant, session_id)
            now = float(self.hub.clock())
            require_available(conn, session, now, self.hub.offline_after)
            request_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO hub_session_close_requests(request_id,tenant,actor,client_id,"
                "idempotency_key,instance_id,generation,session_id,local_session_id,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)",
                (request_id, tenant, actor, client_id, key, session["instance_id"],
                 session["generation"], session_id, session["local_session_id"], now, now))
            conn.execute(
                "UPDATE hub_sessions SET status='closing',controllable=0,frozen=1,updated_at=? "
                "WHERE tenant=? AND session_id=?", (now, tenant, session_id))
            return request_record(request_row(conn, tenant, request_id))

    def get(self, tenant, request_id):
        tenant, request_id = identifier(tenant, "tenant"), identifier(request_id, "request_id")
        conn = self.hub._connect()
        try:
            return request_record(request_row(conn, tenant, request_id))
        finally:
            conn.close()

    def pending(self, tenant, instance, generation):
        tenant, instance = identifier(tenant, "tenant"), identifier(instance, "instance")
        conn = self.hub._connect()
        try:
            conn.execute("BEGIN")
            require_live_generation(conn, tenant, instance, generation,
                                    float(self.hub.clock()), self.hub.offline_after)
            rows = conn.execute(
                "SELECT * FROM hub_session_close_requests WHERE tenant=? AND instance_id=? "
                "AND generation=? AND status='queued' ORDER BY created_at,rowid LIMIT 8",
                (tenant, instance, generation)).fetchall()
            return [command_record(row) for row in rows]
        finally:
            conn.close()

    def report(self, tenant, instance, generation, body):
        tenant, instance = identifier(tenant, "tenant"), identifier(instance, "instance")
        if not isinstance(body, dict) or set(body) - {
                "request_id", "status", "error_code", "instance_id", "generation"}:
            raise ValueError("unsupported session close report fields")
        request_id = identifier(body.get("request_id"), "request_id")
        status, error = report_result(body)
        with self.hub._write() as conn:
            now = float(self.hub.clock())
            require_live_generation(conn, tenant, instance, generation, now, self.hub.offline_after)
            row = request_row(conn, tenant, request_id)
            if row["instance_id"] != instance:
                raise HubStoreError("not_found", "session close request not found", 404)
            if row["generation"] != generation:
                raise HubStoreError("stale_generation", "session close belongs to an earlier generation")
            return complete_request(conn, row, status, error, now)
