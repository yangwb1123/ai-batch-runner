"""Atomic compute admissions, state events, cancellation and restart recovery."""

from __future__ import annotations

import json
import uuid

from .hub_compute_models import TASK_SCHEMA, TERMINAL, task_record
from .hub_store_helpers import (HubStoreError, append_internal_event, canonical_json,
                                get_session, identifier)


class ComputeStore:
    def __init__(self, hub):
        self.hub = hub
        with hub._write() as conn:
            # executescript commits implicitly; run statements within the owner transaction.
            for statement in TASK_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)

    def submit(self, principal, session: dict, body: dict, key: str,
               targets: list[str], archive: bool, *, workspace_input=None) -> dict:
        key = identifier(key, "idempotency_key")
        request = canonical_json({"session_id": session["session_id"], **body})
        now = float(self.hub.clock())
        with self.hub._write() as conn:
            prior = conn.execute(
                "SELECT * FROM hub_compute_tasks WHERE tenant=? AND actor=? AND idempotency_key=?",
                (principal.tenant_id, principal.subject, key)).fetchone()
            if prior:
                if prior["request_json"] != request:
                    raise HubStoreError("idempotency_conflict", "task key has different content")
                return task_record(prior)
            fresh = get_session(conn, principal.tenant_id, session["session_id"])
            if fresh["frozen"] or not fresh["controllable"]:
                raise HubStoreError("session_unavailable", "session cannot submit compute tasks")
            self._check_delegation(conn, principal, fresh, body)
            self._check_capacity(conn, principal)
            task_id = "compute-" + uuid.uuid4().hex
            data = dict(body, project_id=fresh["project_id"], device_id="", device_instance_id="",
                        error="", result={}, artifact_ref={},
                        archive_state="pending" if archive else "disabled",
                        _targets=targets, _fabric_id="", _cwd="")
            if workspace_input is not None:
                data.update(_workspace_input=workspace_input, workspace_result={
                    "input_sha256": workspace_input["sha256"], "output_sha256": "",
                    "state": "pending", "artifact_ref": {}})
            conn.execute("INSERT INTO hub_compute_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (task_id, principal.tenant_id, principal.subject, key, request,
                          fresh["session_id"], fresh["instance_id"], fresh["generation"],
                          "queued", now, now, 0, canonical_json(data)))
            row = self._row(conn, principal.tenant_id, task_id)
            self._event(conn, row, "queued", now)
            return task_record(row)

    @staticmethod
    def _check_delegation(conn, principal, session, body):
        if principal.kind != "delegated":
            return
        row = conn.execute("SELECT status,generation,actor FROM hub_turns WHERE tenant=? AND turn_id=? "
                           "AND session_id=?", (principal.tenant_id, body["turn_id"],
                                                session["session_id"])).fetchone()
        if (row is None or row["status"] != "running" or row["generation"] != session["generation"]
                or row["actor"] != principal.subject):
            raise HubStoreError("delegation_expired", "turn no longer accepts compute tasks", 403)

    @staticmethod
    def _check_capacity(conn, principal):
        active = "state NOT IN ('completed','failed','cancelled','lost')"
        for predicate, args, ceiling in (
                ("1=1", (), 1000),
                ("tenant=?", (principal.tenant_id,), 100),
                ("tenant=? AND actor=?", (principal.tenant_id, principal.subject), 32)):
            count = conn.execute("SELECT COUNT(*) FROM hub_compute_tasks WHERE " + active +
                                 " AND " + predicate, args).fetchone()[0]
            if count >= ceiling:
                raise HubStoreError("queue_full", "compute queue admission quota reached", 429)

    @staticmethod
    def _row(conn, tenant: str, task_id: str):
        row = conn.execute("SELECT * FROM hub_compute_tasks WHERE tenant=? AND task_id=?",
                           (tenant, task_id)).fetchone()
        if row is None:
            raise HubStoreError("not_found", "compute task not found", 404)
        return row

    def get(self, tenant: str, task_id: str, *, private=False) -> dict:
        conn = self.hub._connect()
        try:
            return task_record(self._row(conn, tenant, task_id), private=private)
        finally:
            conn.close()

    def list(self, tenant: str, allowed, session_id="", after="", limit=100) -> list:
        query = "SELECT * FROM hub_compute_tasks WHERE tenant=? AND task_id>?"
        args = [tenant, after]
        if session_id:
            query += " AND session_id=?"
            args.append(session_id)
        if allowed is not None:
            query += " AND instance_id IN (" + ",".join("?" for _ in allowed) + ")"
            args.extend(allowed)
        query += " ORDER BY task_id LIMIT ?"
        conn = self.hub._connect()
        try:
            return [task_record(row) for row in conn.execute(query, [*args, limit])]
        finally:
            conn.close()

    def pending(self, limit=64, *, archive=False) -> list:
        conn = self.hub._connect()
        terminal = "state IN ('completed','failed','cancelled','lost')"
        selector = (terminal + " AND json_extract(data_json,'$.archive_state') "
                    "IN ('pending','failed')") if archive else "NOT (" + terminal + ")"
        try:
            rows = conn.execute(
                "SELECT * FROM hub_compute_tasks WHERE retry_at<=? AND (" + selector + ") "
                "ORDER BY CASE state WHEN 'cancel_requested' THEN 0 ELSE 1 END, "
                "retry_at,updated_at LIMIT ?", (self.hub.clock(), limit))
            return [task_record(row, private=True) for row in rows]
        finally:
            conn.close()

    def update(self, tenant: str, task_id: str, changes: dict, *, state="",
               delay=1.0, expected=()) -> dict:
        now = float(self.hub.clock())
        with self.hub._write() as conn:
            row = self._row(conn, tenant, task_id)
            if expected and row["state"] not in expected:
                return task_record(row, private=True)
            chosen = state or row["state"]
            if row["state"] == "cancel_requested" and chosen not in TERMINAL:
                chosen = "cancel_requested"
            if row["state"] == "running" and chosen == "dispatching":
                chosen = "running"
            if row["state"] in TERMINAL and chosen != row["state"]:
                return task_record(row, private=True)
            data = json.loads(row["data_json"])
            data.update(changes)
            conn.execute("UPDATE hub_compute_tasks SET state=?,data_json=?,updated_at=?,retry_at=? "
                         "WHERE tenant=? AND task_id=?",
                         (chosen, canonical_json(data), now, now + delay, tenant, task_id))
            if chosen != row["state"]:
                self._event(conn, row, chosen, now)
            return task_record(self._row(conn, tenant, task_id), private=True)

    def requeue_unstarted(self, tenant: str, task_id: str, *, reason: str,
                          delay: float = 0.0) -> dict:
        """Return a dispatching task to placement after a pre-start loss.

        The remote task must have no start evidence before the caller invokes
        this method.  Keeping the state transition here makes the cleanup of
        the old Fabric handle atomic with the new queued state; a late remote
        response therefore cannot cause the Hub to submit the same handle
        again.
        """
        if not isinstance(reason, str) or not reason or len(reason) > 128:
            raise ValueError("requeue reason must be a bounded string")
        now = float(self.hub.clock())
        with self.hub._write() as conn:
            row = self._row(conn, tenant, task_id)
            if row["state"] != "dispatching":
                return task_record(row, private=True)
            data = json.loads(row["data_json"])
            try:
                attempt = int(data.get("_fabric_attempt", 0)) + 1
            except (TypeError, ValueError):
                raise HubStoreError("invalid dispatch attempt metadata",
                                     "compute dispatch metadata is invalid")
            request_id = f"{task_id}:attempt-{attempt}"
            data.update({"device_id": "", "device_instance_id": "",
                         "error": reason, "_fabric_id": "", "_cwd": "",
                         "_dispatch_uncertain": False,
                         "_fabric_attempt": attempt,
                         "_fabric_request_id": request_id})
            conn.execute(
                "UPDATE hub_compute_tasks SET state='queued',data_json=?,"
                "updated_at=?,retry_at=? WHERE tenant=? AND task_id=?",
                (canonical_json(data), now, now + max(0.0, float(delay)),
                 tenant, task_id))
            self._event(conn, row, "queued", now, reason=reason)
            return task_record(self._row(conn, tenant, task_id), private=True)

    def retry_lost(self, tenant: str, task_id: str, target_device_id: str, *,
                   actor="", confirm_duplicate=False) -> dict:
        """Explicitly re-admit a terminal lost task after duplicate-risk approval.

        A lost Fabric task can have run even when its evidence never reached the
        Hub.  This transition is therefore never automatic: callers must pass
        the explicit confirmation and a task with disabled evidence archiving.
        The old remote handle is removed before the task becomes visible to the
        dispatcher, and a fresh request id fences Fabric's idempotency record.
        """
        if confirm_duplicate is not True:
            raise HubStoreError("confirmation_required",
                                "retry requires duplicate-side-effect confirmation", 400)
        now = float(self.hub.clock())
        with self.hub._write() as conn:
            row = self._row(conn, tenant, task_id)
            if actor and row["actor"] != actor:
                raise HubStoreError("forbidden", "only the task actor may retry it", 403)
            if row["state"] != "lost":
                raise HubStoreError("task_not_lost", "only lost tasks can be retried", 409)
            data = json.loads(row["data_json"])
            if data.get("archive_state", "disabled") != "disabled":
                raise HubStoreError("archive_in_progress",
                                    "archived tasks cannot be retried", 409)
            data = self._retry_data(data, task_id, target_device_id)
            conn.execute(
                "UPDATE hub_compute_tasks SET state='queued',data_json=?,"
                "updated_at=?,retry_at=? WHERE tenant=? AND task_id=?",
                (canonical_json(data), now, now, tenant, task_id))
            self._event(conn, row, "queued", now, trigger_subject=actor,
                        reason="retry_requested")
            return task_record(self._row(conn, tenant, task_id), private=True)

    @staticmethod
    def _retry_data(data: dict, task_id: str, target_device_id: str) -> dict:
        try:
            attempt = int(data.get("_fabric_attempt", 0)) + 1
        except (TypeError, ValueError):
            raise HubStoreError("invalid dispatch attempt metadata",
                                 "compute dispatch metadata is invalid")
        data.update({"target_device_id": target_device_id, "device_id": "",
                     "device_instance_id": "", "error": "retry_requested",
                     "result": {}, "artifact_ref": {}, "_fabric_id": "",
                     "_cwd": "", "_fabric_actor": "", "_dispatch_uncertain": False,
                     "_fabric_attempt": attempt,
                     "_fabric_request_id": f"{task_id}:retry-{attempt}"})
        data.pop("gpu_assignment", None)
        previous = data.get("workspace_result")
        if isinstance(previous, dict):
            data["workspace_result"] = {
                "input_sha256": previous.get("input_sha256", ""),
                "output_sha256": "", "state": "pending", "artifact_ref": {}}
        data.pop("_workspace_output", None)
        return data

    def cancel(self, tenant: str, task_id: str, *, actor="") -> dict:
        with self.hub._write() as conn:
            row = self._row(conn, tenant, task_id)
            if row["state"] not in TERMINAL | {"cancel_requested"}:
                state = "cancelled" if row["state"] == "queued" else "cancel_requested"
                now = float(self.hub.clock())
                conn.execute("UPDATE hub_compute_tasks SET state=?,updated_at=?,retry_at=0 "
                             "WHERE tenant=? AND task_id=?", (state, now, tenant, task_id))
                self._event(conn, row, state, now, trigger_subject=actor)
            return task_record(self._row(conn, tenant, task_id))

    def reschedule(self, tenant: str, task_id: str, target_device_id: str, *, actor="") -> dict:
        """Atomically change the target of work that has not been dispatched."""
        with self.hub._write() as conn:
            row = self._row(conn, tenant, task_id)
            if actor and row["actor"] != actor:
                raise HubStoreError("forbidden", "only the task actor may reschedule it", 403)
            if row["state"] != "queued":
                raise HubStoreError("task_not_queued", "only queued tasks can be rescheduled", 409)
            data = json.loads(row["data_json"])
            if data.get("target_device_id", "") == target_device_id:
                return task_record(row)
            data["target_device_id"] = target_device_id
            data["device_id"] = ""
            data["device_instance_id"] = ""
            data["error"] = ""
            for key in ("_cwd", "_fabric_id", "_fabric_actor", "_dispatch_uncertain"):
                data.pop(key, None)
            now = float(self.hub.clock())
            conn.execute("UPDATE hub_compute_tasks SET state='queued',data_json=?,updated_at=?,retry_at=? "
                         "WHERE tenant=? AND task_id=?",
                         (canonical_json(data), now, now, tenant, task_id))
            details = dict(row, turn_id=data.get("turn_id", ""))
            append_internal_event(
                conn, tenant, details, str(uuid.uuid4()), "compute.queued",
                {"task_id": task_id, "state": "queued", "trigger_subject": actor,
                 "reason": "rescheduled", "target_device_id": target_device_id}, now)
            return task_record(self._row(conn, tenant, task_id))

    def begin_dispatch(self, tenant, task_id):
        """Linearize first dispatch against cancellation after slow input reads."""
        with self.hub._write() as conn:
            row = self._row(conn, tenant, task_id)
            data = json.loads(row["data_json"])
            uncertain = data.get("_dispatch_uncertain", False)
            state = row["state"]
            if state == "cancel_requested" and not uncertain and not data.get("_fabric_id"):
                state = "cancelled"
            if state not in TERMINAL:
                data["_dispatch_uncertain"] = True
            now = float(self.hub.clock())
            conn.execute("UPDATE hub_compute_tasks SET state=?,data_json=?,updated_at=? "
                         "WHERE tenant=? AND task_id=?",
                         (state, canonical_json(data), now, tenant, task_id))
            if state != row["state"]:
                self._event(conn, row, state, now)
            return task_record(self._row(conn, tenant, task_id), private=True), uncertain

    @staticmethod
    def _event(conn, row, state: str, now: float, *, trigger_subject="",
               reason=""):
        details = json.loads(row["data_json"])
        context = dict(row, turn_id=details.get("turn_id", ""))
        payload = {"task_id": row["task_id"], "state": state,
                   "trigger_subject": trigger_subject}
        if reason:
            payload["reason"] = reason
        append_internal_event(conn, row["tenant"], context, str(uuid.uuid4()),
                              "compute." + state, payload, now)
