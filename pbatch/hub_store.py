"""SQLite WAL persistence for one session Hub, with tenant and generation fencing."""

from __future__ import annotations

import contextlib
import fcntl
import math
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator

from .hub_store_helpers import (
    MAX_EVENT_BYTES, MAX_EVENT_PAGE, MAX_PAGE,
    MAX_PROMPT_BYTES, _SCHEMA, _SESSION_NAMESPACE, _TERMINAL, HubStoreError,
    _TURN_EVENT_STATES, allowed_instance_ids as _allowed_instance_ids,
    append_event_tx as _append_event_tx,
    append_internal_event as _append_internal_event,
    append_queued_event as _append_queued_event, command_record as _command_record,
    apply_turn_event as _apply_turn_event, boolean as _boolean,
    bounded_text as _text, canonical_json as _json,
    check_session_owner as _check_session_owner,
    ensure_no_active_turn as _ensure_no_active_turn,
    event_record as _event_record, get_session as _get_session,
    get_turn_for_session as _get_turn_for_session,
    global_session_id as _global_session_id, identifier as _identifier,
    instance_record as _instance_record, nonnegative_int as _nonnegative_int,
    page_limit as _limit, require_generation as _require_generation,
    require_live_generation as _require_live_generation,
    safe_object as _safe_object, session_record as _session_record,
    turn_record as _turn_record,
)
from .hub_session_request_models import (
    SCHEMA as _SESSION_REQUEST_SCHEMA,
    guard_session_registration as _guard_session_registration,
)
from .hub_session_request_store import SessionRequestStore
from .hub_session_close_models import SCHEMA as _SESSION_CLOSE_SCHEMA
from .hub_session_close_store import SessionCloseStore
from .hub_instance_registry import register_instance as _register_instance
from .hub_session_operations_store import SCHEMA as _OPERATIONS_SCHEMA, list_operations


class HubStore:
    """WAL-backed durable registry, turn queue, and per-tenant event log."""

    def __init__(self, path, *, clock: Callable[[], float] = time.time,
                 offline_after: float = 60.0) -> None:
        if (isinstance(offline_after, bool) or
                not isinstance(offline_after, (int, float)) or
                not math.isfinite(float(offline_after)) or offline_after <= 0):
            raise ValueError("offline_after must be positive")
        raw_path = str(path or "").strip()
        if not raw_path or raw_path == ":memory:":
            raise ValueError("HubStore requires a durable SQLite file path")
        self.path = Path(raw_path).expanduser().absolute()
        self.clock = clock
        self.offline_after = float(offline_after)
        self._closed = False
        self._lock_fd = None
        self._session_creation = SessionRequestStore(self)
        self._session_closing = SessionCloseStore(self)
        self._prepare_path()
        try:
            self._initialize()
        except Exception:
            self._release_lock()
            raise

    def _prepare_path(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("HubStore database must not be a symlink")
        if self.path.exists() and not self.path.is_file():
            raise ValueError("HubStore database must be a regular file")
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        lock_path = Path(str(self.path) + ".lock")
        if lock_path.is_symlink():
            raise ValueError("HubStore lock must not be a symlink")
        self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._release_lock()
            raise HubStoreError("store_in_use", "another HubStore owns this database") from exc

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise HubStoreError("store_closed", "HubStore is closed", 503)
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            conn.executescript(_SESSION_REQUEST_SCHEMA)
            conn.executescript(_SESSION_CLOSE_SCHEMA)
            conn.executescript(_OPERATIONS_SCHEMA)
            from .hub_compute_models import AUTH_SCHEMA
            conn.executescript(AUTH_SCHEMA)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported HubStore schema version")
            conn.execute("PRAGMA user_version = 1")
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def close(self) -> None:
        """Prevent new operations; each transaction already owns its connection."""
        self._closed = True
        self._release_lock()

    def _release_lock(self) -> None:
        if self._lock_fd is None:
            return
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        self._lock_fd = None

    def register_instance(self, tenant: str, instance: str,
                          body: dict) -> dict:
        return _register_instance(self, tenant, instance, body)

    def heartbeat(self, tenant: str, instance: str, generation: int) -> dict:
        tenant = _identifier(tenant, "tenant")
        instance = _identifier(instance, "instance")
        now = float(self.clock())
        with self._write() as conn:
            _require_generation(conn, tenant, instance, generation)
            conn.execute(
                "UPDATE hub_instances SET last_seen=? WHERE tenant=? AND instance_id=?",
                (now, tenant, instance))
            updated = conn.execute(
                "SELECT * FROM hub_instances WHERE tenant=? AND instance_id=?",
                (tenant, instance)).fetchone()
            return _instance_record(updated, now, self.offline_after)

    def upsert_session(self, tenant: str, instance: str, generation: int,
                       body: dict) -> dict:
        tenant = _identifier(tenant, "tenant")
        instance = _identifier(instance, "instance")
        if not isinstance(body, dict):
            raise ValueError("session body must be an object")
        local_id = _identifier(body.get("local_session_id"), "local_session_id")
        name = _text(body.get("name", ""), "name", 256)
        project_id = _text(body.get("project_id", ""), "project_id", 256)
        controllable = _boolean(body.get("controllable", False), "controllable")
        status = _text(body.get("status", "active"), "status", 32) or "active"
        session_id = _global_session_id(tenant, instance, local_id)
        now = float(self.clock())
        with self._write() as conn:
            inst = _require_live_generation(
                conn, tenant, instance, generation, now, self.offline_after)
            prior = conn.execute(
                "SELECT * FROM hub_sessions WHERE tenant=? AND instance_id=? AND local_session_id=?",
                (tenant, instance, local_id)).fetchone()
            if prior and (bool(prior["frozen"]) or
                          int(prior["generation"]) != int(inst["generation"])):
                raise HubStoreError("session_read_only", "old-generation session is read-only")
            _guard_session_registration(conn, inst, local_id, prior)
            conn.execute(
                "INSERT INTO hub_sessions(session_id,tenant,instance_id,local_session_id,generation,"
                "name,project_id,controllable,status,frozen,updated_at) VALUES(?,?,?,?,?,?,?,?,?,0,?) "
                "ON CONFLICT(tenant,instance_id,local_session_id) DO UPDATE SET "
                "name=excluded.name,project_id=excluded.project_id,controllable=excluded.controllable,"
                "status=excluded.status,updated_at=excluded.updated_at",
                (session_id, tenant, instance, local_id, generation, name,
                 project_id, int(controllable), status, now))
            row = conn.execute(
                "SELECT * FROM hub_sessions WHERE tenant=? AND session_id=?",
                (tenant, session_id)).fetchone()
            return _session_record(conn, row, now, self.offline_after)

    def create_session(self, tenant, instance, actor, name, idempotency_key, *, client_id):
        return self._session_creation.create(
            tenant, instance, actor, name, idempotency_key, client_id=client_id)

    def session_request(self, tenant, request_id):
        return self._session_creation.get(tenant, request_id)

    def session_requests(self, tenant, instance, generation):
        return self._session_creation.pending(tenant, instance, generation)

    def report_session_request(self, tenant, instance, generation, body):
        return self._session_creation.report(tenant, instance, generation, body)

    def close_session(self, tenant, session_id, actor, idempotency_key, *, client_id):
        return self._session_closing.close(
            tenant, session_id, actor, idempotency_key, client_id=client_id)

    def session_close_request(self, tenant, request_id):
        return self._session_closing.get(tenant, request_id)

    def session_close_requests(self, tenant, instance, generation):
        return self._session_closing.pending(tenant, instance, generation)

    def report_session_close_request(self, tenant, instance, generation, body):
        return self._session_closing.report(tenant, instance, generation, body)

    def session_operations(self, tenant, instance_id="", *, before="", limit=20,
                           allowed_instances=None):
        return list_operations(self, tenant, instance_id, before=before, limit=limit,
                               allowed_instances=allowed_instances)

    def instances(self, tenant: str, limit: int = 100, after: str = "",
                  allowed_instances: tuple[str, ...] | None = None) -> list[dict]:
        tenant = _identifier(tenant, "tenant")
        limit = _limit(limit, MAX_PAGE)
        after = _text(after, "after", 256)
        allowed = _allowed_instance_ids(allowed_instances)
        if allowed == ():
            return []
        query = "SELECT * FROM hub_instances WHERE tenant=? AND instance_id>?"
        values: list[object] = [tenant, after]
        if allowed is not None:
            query += " AND instance_id IN (" + ",".join("?" for _ in allowed) + ")"
            values.extend(allowed)
        query += " ORDER BY instance_id LIMIT ?"
        values.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(query, values).fetchall()
            now = float(self.clock())
            return [_instance_record(row, now, self.offline_after) for row in rows]
        finally:
            conn.close()

    def sessions(self, tenant: str, instance_id: str = "", limit: int = 100,
                 after: str = "",
                 allowed_instances: tuple[str, ...] | None = None) -> list[dict]:
        tenant = _identifier(tenant, "tenant")
        instance_id = _text(instance_id, "instance_id", 256)
        limit = _limit(limit, MAX_PAGE)
        after = _text(after, "after", 256)
        allowed = _allowed_instance_ids(allowed_instances)
        if allowed == ():
            return []
        query = "SELECT * FROM hub_sessions WHERE tenant=? AND session_id>?"
        values: list[object] = [tenant, after]
        if instance_id:
            query += " AND instance_id=?"
            values.append(instance_id)
        if allowed is not None:
            query += " AND instance_id IN (" + ",".join("?" for _ in allowed) + ")"
            values.extend(allowed)
        query += " ORDER BY session_id LIMIT ?"
        values.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(query, values).fetchall()
            now = float(self.clock())
            return [_session_record(conn, row, now, self.offline_after) for row in rows]
        finally:
            conn.close()

    def session(self, tenant: str, session_id: str) -> dict:
        tenant = _identifier(tenant, "tenant")
        session_id = _identifier(session_id, "session_id")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM hub_sessions WHERE tenant=? AND session_id=?",
                (tenant, session_id)).fetchone()
            if row is None:
                raise HubStoreError("not_found", "session not found", 404)
            return _session_record(conn, row, float(self.clock()), self.offline_after)
        finally:
            conn.close()

    def submit_turn(self, tenant: str, session_id: str, actor: str,
                    prompt: str, idempotency_key: str, *, authority: dict | None = None) -> dict:
        tenant = _identifier(tenant, "tenant")
        session_id = _identifier(session_id, "session_id")
        actor = _identifier(actor, "actor")
        prompt = _text(prompt, "prompt", MAX_PROMPT_BYTES, by_bytes=True)
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        idempotency_key = _identifier(idempotency_key, "idempotency_key", 256)
        now = float(self.clock())
        with self._write() as conn:
            prior = conn.execute(
                "SELECT * FROM hub_turns WHERE tenant=? AND actor=? AND idempotency_key=?",
                (tenant, actor, idempotency_key)).fetchone()
            if prior:
                if (prior["session_id"], prior["actor"], prior["prompt"]) != (
                        session_id, actor, prompt):
                    raise HubStoreError("idempotency_conflict", "turn key has different content")
                return _turn_record(prior)
            session = _get_session(conn, tenant, session_id)
            view = _session_record(conn, session, now, self.offline_after)
            if not view["controllable"]:
                raise HubStoreError("session_unavailable", "session is not currently controllable", 409)
            _ensure_no_active_turn(conn, tenant, session_id)
            turn_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO hub_turns(turn_id,tenant,session_id,instance_id,generation,actor,prompt,"
                "idempotency_key,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'queued',?,?)",
                (turn_id, tenant, session_id, session["instance_id"],
                 session["generation"], actor, prompt, idempotency_key, now, now))
            command_id = str(uuid.uuid5(_SESSION_NAMESPACE, "prompt:" + turn_id))
            conn.execute(
                "INSERT INTO hub_commands(command_id,tenant,instance_id,generation,session_id,turn_id,"
                "kind,status,created_at) VALUES(?,?,?,?,?,?,'prompt','pending',?)",
                (command_id, tenant, session["instance_id"], session["generation"],
                 session_id, turn_id, now))
            row = conn.execute(
                "SELECT * FROM hub_turns WHERE tenant=? AND turn_id=?",
                (tenant, turn_id)).fetchone()
            from .hub_compute_models import save_authority
            save_authority(conn, turn_id, authority)
            _append_queued_event(conn, tenant, row, now)
            return _turn_record(row)

    def turn(self, tenant: str, turn_id: str) -> dict:
        tenant = _identifier(tenant, "tenant")
        turn_id = _identifier(turn_id, "turn_id")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM hub_turns WHERE tenant=? AND turn_id=?",
                (tenant, turn_id)).fetchone()
            if row is None:
                raise HubStoreError("not_found", "turn not found", 404)
            return _turn_record(row)
        finally:
            conn.close()

    def commands(self, tenant: str, instance: str,
                 generation: int) -> list[dict]:
        tenant = _identifier(tenant, "tenant")
        instance = _identifier(instance, "instance")
        conn = self._connect()
        try:
            _require_live_generation(
                conn, tenant, instance, generation,
                float(self.clock()), self.offline_after)
            rows = conn.execute(
                "SELECT c.*,t.status AS turn_state,t.prompt,s.local_session_id "
                "FROM hub_commands c JOIN hub_turns t ON t.tenant=c.tenant AND t.turn_id=c.turn_id "
                "JOIN hub_sessions s ON s.tenant=c.tenant AND s.session_id=c.session_id "
                "WHERE c.tenant=? AND c.instance_id=? AND c.generation=? AND c.status='pending' "
                "ORDER BY c.created_at,c.command_id LIMIT ?",
                (tenant, instance, generation, MAX_PAGE)).fetchall()
            return [_command_record(row) for row in rows]
        finally:
            conn.close()

    def append_event(self, tenant: str, instance: str,
                     generation: int, body: dict) -> dict:
        tenant = _identifier(tenant, "tenant")
        instance = _identifier(instance, "instance")
        if not isinstance(body, dict):
            raise ValueError("event body must be an object")
        event_id = _identifier(body.get("event_id"), "event_id", 256)
        session_id = _identifier(body.get("session_id"), "session_id")
        turn_id = _text(body.get("turn_id", ""), "turn_id", 256)
        kind = _identifier(body.get("kind"), "kind", 128)
        if kind in _TURN_EVENT_STATES and not turn_id:
            raise ValueError("turn state events require turn_id")
        payload = _safe_object(body.get("payload", {}), MAX_EVENT_BYTES, "event payload")
        canonical = _json({"session_id": session_id, "turn_id": turn_id,
                           "kind": kind, "payload": payload})
        if len(canonical.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("event exceeds size limit")
        now = float(self.clock())
        with self._write() as conn:
            _require_live_generation(
                conn, tenant, instance, generation, now, self.offline_after)
            session = _get_session(conn, tenant, session_id)
            _check_session_owner(session, instance, generation, allow_frozen=True)
            prior = conn.execute(
                "SELECT * FROM hub_events WHERE tenant=? AND event_id=?",
                (tenant, event_id)).fetchone()
            if not prior and session["frozen"]:
                raise HubStoreError("session_read_only", "session no longer accepts new events")
            turn = _get_turn_for_session(conn, tenant, turn_id, session_id) if turn_id else None
            if prior:
                if prior["canonical_json"] != canonical:
                    raise HubStoreError("event_conflict", "event id has different content")
                return dict(_event_record(prior, duplicate=True), turn_state=turn["status"] if turn else "")
            event = _append_event_tx(
                conn, tenant, instance, generation, event_id, session_id,
                turn_id, kind, payload, canonical, now)
            if turn_id and kind in _TURN_EVENT_STATES:
                _apply_turn_event(conn, tenant, turn_id, kind, now)
            turn = _get_turn_for_session(conn, tenant, turn_id, session_id) if turn_id else None
            return dict(event, turn_state=turn["status"] if turn else "")

    def cancel_turn(self, tenant: str, turn_id: str, actor: str) -> dict:
        tenant = _identifier(tenant, "tenant")
        turn_id = _identifier(turn_id, "turn_id")
        actor = _identifier(actor, "actor")
        now = float(self.clock())
        with self._write() as conn:
            row = conn.execute(
                "SELECT t.*,s.generation AS session_generation,s.frozen,i.generation AS current_generation "
                "FROM hub_turns t JOIN hub_sessions s ON s.tenant=t.tenant AND s.session_id=t.session_id "
                "JOIN hub_instances i ON i.tenant=t.tenant AND i.instance_id=t.instance_id "
                "WHERE t.tenant=? AND t.turn_id=?", (tenant, turn_id)).fetchone()
            if row is None:
                raise HubStoreError("not_found", "turn not found", 404)
            status = row["status"]
            if status in _TERMINAL:
                if status == "cancelled":
                    return _turn_record(row)
                raise HubStoreError("turn_terminal", "turn is already terminal")
            if status == "cancel_requested":
                return _turn_record(row)
            if (row["frozen"] or int(row["generation"]) != int(row["current_generation"]) or
                    int(row["session_generation"]) != int(row["current_generation"])):
                raise HubStoreError("stale_generation", "turn owner generation is stale")
            if status == "queued":
                self._cancel_queued(conn, tenant, turn_id, actor, row, now)
            elif status == "running":
                self._request_cancel(conn, tenant, turn_id, actor, row, now)
            else:
                raise HubStoreError("turn_state", "turn cannot be cancelled in its current state")
            updated = conn.execute(
                "SELECT * FROM hub_turns WHERE tenant=? AND turn_id=?",
                (tenant, turn_id)).fetchone()
            return _turn_record(updated)

    def _cancel_queued(self, conn: sqlite3.Connection, tenant: str,
                       turn_id: str, actor: str, row: sqlite3.Row,
                       now: float) -> None:
        conn.execute(
            "UPDATE hub_turns SET status='cancelled',updated_at=? WHERE tenant=? AND turn_id=?",
            (now, tenant, turn_id))
        conn.execute(
            "UPDATE hub_commands SET status='resolved' WHERE tenant=? AND turn_id=?",
            (tenant, turn_id))
        event_id = str(uuid.uuid5(_SESSION_NAMESPACE, "cancelled:" + turn_id))
        _append_internal_event(conn, tenant, row, event_id, "turn.cancelled",
                               {"actor": actor}, now)

    def _request_cancel(self, conn: sqlite3.Connection, tenant: str,
                        turn_id: str, actor: str, row: sqlite3.Row,
                        now: float) -> None:
        conn.execute(
            "UPDATE hub_turns SET status='cancel_requested',updated_at=? WHERE tenant=? AND turn_id=?",
            (now, tenant, turn_id))
        command_id = str(uuid.uuid5(_SESSION_NAMESPACE, "cancel:" + turn_id))
        conn.execute(
            "INSERT OR IGNORE INTO hub_commands(command_id,tenant,instance_id,generation,session_id,"
            "turn_id,kind,status,created_at) VALUES(?,?,?,?,?,?,'cancel','pending',?)",
            (command_id, tenant, row["instance_id"], row["generation"],
             row["session_id"], turn_id, now))
        event_id = str(uuid.uuid5(_SESSION_NAMESPACE, "cancel-requested:" + turn_id))
        _append_internal_event(conn, tenant, row, event_id,
                               "turn.cancel_requested", {"actor": actor}, now)

    def events(self, tenant: str, session_id: str, after: int = 0,
               limit: int = 100) -> list[dict]:
        tenant = _identifier(tenant, "tenant")
        session_id = _identifier(session_id, "session_id")
        after = _nonnegative_int(after, "after")
        limit = _limit(limit, MAX_EVENT_PAGE)
        conn = self._connect()
        try:
            _get_session(conn, tenant, session_id)
            rows = conn.execute(
                "SELECT * FROM hub_events WHERE tenant=? AND session_id=? AND cursor>? "
                "ORDER BY cursor LIMIT ?", (tenant, session_id, after, limit)).fetchall()
            return [_event_record(row) for row in rows]
        finally:
            conn.close()
