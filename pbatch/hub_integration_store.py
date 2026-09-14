"""Bounded replay into a persistent outbox with immutable targets and retry receipts."""

from __future__ import annotations

import uuid

from .hub_ecosystem_http import IntegrationError, canonical
from .hub_integration_models import SCHEMA, delivery_id, delivery_record, lifecycle_fact
from .hub_store_helpers import HubStoreError, identifier


MAX_ATTEMPTS = 12
MAX_PENDING = 1000
IM_RETRY_WINDOW = 6 * 86400


class IntegrationStore:
    def __init__(self, hub):
        self.hub = hub
        with hub._write() as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            # HubStore's exclusive process lock fences the previous dispatcher.
            conn.execute("UPDATE hub_integration_deliveries SET state='retrying',retry_at=0,"
                         "attempt_token='' WHERE state='sending'")

    def bind(self, sink, start_cursor=None):
        now = self.hub.clock()
        with self.hub._write() as conn:
            row = conn.execute("SELECT * FROM hub_integration_sinks WHERE tenant=? AND name=?",
                               (sink.tenant_id, sink.name)).fetchone()
            if row:
                if (row["kind"], row["fingerprint"]) != (sink.kind, sink.fingerprint):
                    raise ValueError("integration target changed; use a new sink name")
                return
            cursor = start_cursor
            if cursor is None:
                cursor = conn.execute("SELECT COALESCE(MAX(cursor),0) FROM hub_events WHERE tenant=?",
                                      (sink.tenant_id,)).fetchone()[0]
            conn.execute("INSERT INTO hub_integration_sinks VALUES(?,?,?,?,?,?)",
                         (sink.tenant_id, sink.name, sink.kind, sink.fingerprint, cursor, now))

    def project(self, sink, limit=128):
        with self.hub._write() as conn:
            bound = conn.execute("SELECT cursor FROM hub_integration_sinks WHERE tenant=? AND name=?",
                                 (sink.tenant_id, sink.name)).fetchone()
            capacity = self._capacity(conn, sink.tenant_id)
            if capacity <= 0:
                return
            rows = conn.execute("SELECT * FROM hub_events WHERE tenant=? AND cursor>? "
                                "ORDER BY cursor LIMIT ?", (sink.tenant_id, bound[0], limit)).fetchall()
            for row in rows:
                fact = lifecycle_fact(conn, row)
                if fact is not None:
                    capacity -= self._enqueue(conn, sink, fact)
                conn.execute("UPDATE hub_integration_sinks SET cursor=? WHERE tenant=? AND name=?",
                             (row["cursor"], sink.tenant_id, sink.name))
                if capacity <= 0:
                    break

    @staticmethod
    def _capacity(conn, tenant):
        active = "state IN ('pending','sending','retrying')"
        own = conn.execute("SELECT COUNT(*) FROM hub_integration_deliveries WHERE tenant=? AND " +
                           active, (tenant,)).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM hub_integration_deliveries WHERE " + active).fetchone()[0]
        return min(MAX_PENDING - own, 10000 - total)

    def _enqueue(self, conn, sink, fact):
        error, payload = "", None
        try:
            payload = sink.prepare(fact)
            if payload is None:
                return 0
            encoded = self._payload(payload)
        except IntegrationError as exc:
            error, encoded = exc.code, None
        now = self.hub.clock()
        conn.execute(
            "INSERT INTO hub_integration_deliveries(delivery_id,tenant,sink_name,sink_kind,"
            "fingerprint,event_id,cursor,instance_id,kind,state,retry_at,created_at,updated_at,"
            "fact_json,payload_json,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (delivery_id(sink.tenant_id, sink.name, fact["event_id"]), sink.tenant_id, sink.name,
             sink.kind, sink.fingerprint, fact["event_id"], fact["cursor"], fact["instance_id"],
             fact["kind"], "failed" if error else "pending", now, now, now,
             canonical(fact), encoded, error))
        return 0 if error else 1

    @staticmethod
    def _payload(value):
        if not isinstance(value, dict):
            raise IntegrationError("invalid_delivery_payload", retryable=False)
        encoded = canonical(value)
        if len(encoded.encode("utf-8")) > 65536:
            raise IntegrationError("delivery_payload_too_large", retryable=False)
        return encoded

    def due(self, sink, limit=16):
        conn = self.hub._connect()
        try:
            rows = conn.execute("SELECT * FROM hub_integration_deliveries WHERE tenant=? AND sink_name=? "
                                "AND state IN ('pending','retrying') AND retry_at<=? "
                                "ORDER BY retry_at,cursor LIMIT ?",
                                (sink.tenant_id, sink.name, self.hub.clock(), limit))
            return [delivery_record(row, private=True) for row in rows]
        finally:
            conn.close()

    def recover_stalled(self):
        now = self.hub.clock()
        with self.hub._write() as conn:
            conn.execute("UPDATE hub_integration_deliveries SET state='retrying',retry_at=?,"
                         "attempt_token='' WHERE state='sending' AND updated_at<?", (now, now - 90))

    def claim(self, tenant, key):
        with self.hub._write() as conn:
            row = self._row(conn, tenant, key)
            if row["state"] not in {"pending", "retrying"} or row["retry_at"] > self.hub.clock():
                return None
            now, token = self.hub.clock(), str(uuid.uuid4())
            if self._expired(row, now):
                self._expired_update(conn, key, now)
                return None
            if row["attempts"] >= row["attempt_limit"]:
                conn.execute("UPDATE hub_integration_deliveries SET state='failed',"
                             "error_code='retry_budget_exhausted',updated_at=? WHERE delivery_id=?", (now, key))
                return None
            conn.execute("UPDATE hub_integration_deliveries SET state='sending',attempts=attempts+1,"
                         "first_attempt_at=COALESCE(first_attempt_at,?),updated_at=?,attempt_token=? "
                         "WHERE delivery_id=?", (now, now, token, key))
            return delivery_record(self._row(conn, tenant, key), private=True)

    @staticmethod
    def _expired(row, now):
        return (row["sink_kind"] == "im" and row["first_attempt_at"] is not None
                and now - row["first_attempt_at"] >= IM_RETRY_WINDOW)

    @staticmethod
    def _expired_update(conn, key, now):
        conn.execute("UPDATE hub_integration_deliveries SET state='failed',"
                     "error_code='idempotency_window_elapsed',updated_at=? WHERE delivery_id=?", (now, key))

    def finish(self, claimed, *, receipt=None, error=None):
        now = self.hub.clock()
        code, status, state, delay = "", 0, "delivered", 0
        if error is not None:
            code, status = error.code, error.status
            state = "retrying" if error.retryable and claimed["attempts"] < claimed["attempt_limit"] else "failed"
            delay = min(300, 5 * 2 ** min(claimed["attempts"], 6))
        encoded = self._payload(receipt or {})
        with self.hub._write() as conn:
            conn.execute("UPDATE hub_integration_deliveries SET state=?,retry_at=?,updated_at=?,"
                         "receipt_json=?,error_code=?,error_status=?,attempt_token='' "
                         "WHERE delivery_id=? AND state='sending' AND attempt_token=?",
                         (state, now + delay, now, encoded, code, status,
                          claimed["delivery_id"], claimed["attempt_token"]))

    @staticmethod
    def _row(conn, tenant, key):
        row = conn.execute("SELECT * FROM hub_integration_deliveries WHERE tenant=? AND delivery_id=?",
                           (tenant, key)).fetchone()
        if row is None:
            raise HubStoreError("not_found", "delivery not found", 404)
        return row

    def get(self, tenant, key, *, private=False):
        conn = self.hub._connect()
        try:
            return delivery_record(self._row(conn, tenant, key), private=private)
        finally:
            conn.close()

    def list(self, tenant, allowed, *, state="", after=0, limit=100):
        if state and state not in {"pending", "sending", "retrying", "failed", "delivered"}:
            raise ValueError("unsupported delivery state")
        query = "SELECT * FROM hub_integration_deliveries WHERE tenant=? AND sequence>?"
        args = [tenant, after]
        if allowed is not None:
            if not allowed:
                return []
            query += " AND instance_id IN (" + ",".join("?" for _ in allowed) + ")"
            args.extend(allowed)
        if state:
            query += " AND state=?"
            args.append(state)
        conn = self.hub._connect()
        try:
            return [delivery_record(row) for row in conn.execute(
                query + " ORDER BY sequence LIMIT ?", [*args, limit])]
        finally:
            conn.close()

    def retry(self, tenant, key, sink):
        identifier(key, "delivery_id")
        with self.hub._write() as conn:
            row = self._row(conn, tenant, key)
            if sink.fingerprint != row["fingerprint"]:
                raise HubStoreError("target_changed", "delivery target configuration changed")
            if row["state"] not in {"failed", "retrying"}:
                raise HubStoreError("delivery_state", "only failed or waiting deliveries can be retried")
            if self._expired(row, self.hub.clock()):
                raise HubStoreError("idempotency_window_elapsed", "notification replay window expired")
            if self._capacity(conn, tenant) <= 0 and row["state"] == "failed":
                raise HubStoreError("delivery_queue_full", "delivery admission quota reached", 429)
            payload = row["payload_json"]
            if payload is None:
                fact = delivery_record(row, private=True)["fact"]
                payload = self._payload(sink.prepare(fact))
            conn.execute("UPDATE hub_integration_deliveries SET state='pending',retry_at=?,updated_at=?,"
                         "payload_json=?,error_code='',error_status=0,attempt_limit=attempts+? "
                         "WHERE delivery_id=?", (self.hub.clock(), self.hub.clock(), payload, MAX_ATTEMPTS, key))
            return delivery_record(self._row(conn, tenant, key))
