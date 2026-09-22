"""Durable, bounded creation requests for operator-configured Gateway sessions."""

import json
import uuid

from .hub_session_request_models import (
    command_record, complete_request, report_result, request_record, request_row,
    require_capacity, session_name,
)
from .hub_store_helpers import HubStoreError, bounded_text, identifier, require_live_generation


class SessionRequestStore:
    def __init__(self, hub):
        self.hub = hub

    def create(self, tenant, instance, actor, name, key, *, client_id):
        tenant, instance = identifier(tenant, "tenant"), identifier(instance, "instance")
        actor, client_id = identifier(actor, "actor"), identifier(client_id, "client_id")
        key, name = identifier(key, "idempotency_key"), session_name(name)
        with self.hub._write() as conn:
            prior = conn.execute(
                "SELECT * FROM hub_session_requests WHERE tenant=? AND actor=? "
                "AND client_id=? AND idempotency_key=?", (tenant, actor, client_id, key)).fetchone()
            if prior:
                if (prior["instance_id"], prior["name"]) != (instance, name):
                    raise HubStoreError("idempotency_conflict", "session request key has different content")
                return request_record(prior)
            inst = conn.execute("SELECT * FROM hub_instances WHERE tenant=? AND instance_id=?",
                                (tenant, instance)).fetchone()
            if inst is None:
                raise HubStoreError("not_found", "instance not found", 404)
            now = float(self.hub.clock())
            require_live_generation(conn, tenant, instance, inst["generation"], now, self.hub.offline_after)
            require_capacity(conn, inst)
            project = bounded_text(json.loads(inst["metadata_json"]).get("project_id", ""),
                                   "project_id", 256)
            request_id, local_id = str(uuid.uuid4()), str(uuid.uuid4())
            conn.execute(
                "INSERT INTO hub_session_requests(request_id,tenant,actor,client_id,idempotency_key,"
                "instance_id,generation,name,project_id,local_session_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
                (request_id, tenant, actor, client_id, key, instance, inst["generation"], name,
                 project, local_id, now, now))
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
                "SELECT * FROM hub_session_requests WHERE tenant=? AND instance_id=? "
                "AND generation=? AND status='queued' ORDER BY created_at,rowid LIMIT 8",
                (tenant, instance, generation)).fetchall()
            return [command_record(row) for row in rows]
        finally:
            conn.close()

    def report(self, tenant, instance, generation, body):
        tenant, instance = identifier(tenant, "tenant"), identifier(instance, "instance")
        if not isinstance(body, dict) or set(body) - {
                "request_id", "status", "error_code", "instance_id", "generation"}:
            raise ValueError("unsupported session report fields")
        request_id = identifier(body.get("request_id"), "request_id")
        status, error = report_result(body)
        with self.hub._write() as conn:
            now = float(self.hub.clock())
            require_live_generation(conn, tenant, instance, generation, now, self.hub.offline_after)
            row = request_row(conn, tenant, request_id)
            if row["instance_id"] != instance:
                raise HubStoreError("not_found", "session request not found", 404)
            if row["generation"] != generation:
                raise HubStoreError("stale_generation", "session request belongs to an earlier generation")
            return complete_request(conn, row, status, error, now)
