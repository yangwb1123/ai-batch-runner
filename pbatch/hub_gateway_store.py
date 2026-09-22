"""Private SQLite receipts and event outbox for a Hub Gateway instance."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


class GatewayStore:
    """Durable local receipts; prompts are never persisted by this store."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "hub-gateway.sqlite3"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self._db.row_factory = sqlite3.Row
        self._initialize()
        self._initialize_creations()
        self._initialize_closures()

    def _initialize_creations(self) -> None:
        with self._lock, self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS session_creations (
                request_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                local_session_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                project_id TEXT NOT NULL, state TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT '', hub_session_id TEXT NOT NULL DEFAULT '',
                acknowledged INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
            )""")
            self._db.execute("""CREATE INDEX IF NOT EXISTS session_creations_pending
                ON session_creations(generation,updated_at)
                WHERE acknowledged=0 AND state IN ('created','failed')""")

    def _initialize_closures(self) -> None:
        with self._lock, self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS session_closures (
                request_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                local_session_id TEXT NOT NULL, session_id TEXT NOT NULL,
                state TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
                acknowledged INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
                UNIQUE(generation,local_session_id)
            )""")
            self._db.execute("""CREATE INDEX IF NOT EXISTS session_closures_pending
                ON session_closures(generation,updated_at)
                WHERE acknowledged=0 AND state IN ('closed','failed')""")

    def _initialize(self) -> None:
        with self._lock:
            self._db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    local_session_id TEXT PRIMARY KEY,
                    hub_session_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    controllable INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    local_session_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    runtime_turn_id TEXT NOT NULL DEFAULT '',
                    prompt_digest TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    sent_cursor TEXT NOT NULL DEFAULT '',
                    obsolete INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS outbox_pending
                    ON outbox(obsolete, sent_cursor, created_at);
            """)
            self._db.commit()

    def metadata(self, key: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else ""

    def set_metadata(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO metadata(key,value) VALUES(?,?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, str(value)))

    def generation(self) -> int:
        try:
            return max(0, int(self.metadata("generation")))
        except ValueError:
            return 0

    def activate_generation(self, generation: int, *, boot_id: str,
                            previous_boot_id: str) -> None:
        if generation <= 0:
            raise ValueError("hub registration returned an invalid generation")
        with self._lock, self._db:
            if previous_boot_id and previous_boot_id != boot_id:
                self._db.execute(
                    "UPDATE turns SET state='uncertain',updated_at=? "
                    "WHERE state IN ('accepted','starting','executing')",
                    (time.time(),))
                self._db.execute("UPDATE sessions SET controllable=0,status='uncertain'")
                self._db.execute("UPDATE outbox SET obsolete=1 WHERE generation<>?",
                                  (generation,))
                self._db.execute("UPDATE session_creations SET state='lost',updated_at=? "
                                 "WHERE acknowledged=0", (time.time(),))
                self._db.execute("UPDATE session_closures SET state='lost',updated_at=? "
                                 "WHERE acknowledged=0", (time.time(),))
            self._db.execute("INSERT INTO metadata(key,value) VALUES('generation',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (str(generation),))
            self._db.execute("INSERT INTO metadata(key,value) VALUES('boot_id',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (boot_id,))

    def add_session(self, *, local_session_id: str, hub_session_id: str,
                    name: str, project_id: str) -> None:
        with self._lock, self._db:
            closing = self._db.execute("""SELECT 1 FROM session_closures
                WHERE generation=? AND local_session_id=? AND state='accepted'""",
                (self.generation(), local_session_id)).fetchone() is not None
            self._db.execute("""INSERT INTO sessions
                (local_session_id,hub_session_id,name,project_id,status,controllable,created_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(local_session_id) DO UPDATE SET
                    hub_session_id=excluded.hub_session_id,name=excluded.name,
                    project_id=excluded.project_id,status=excluded.status,
                    controllable=excluded.controllable""",
                (local_session_id, hub_session_id, name, project_id,
                 "closing" if closing else "idle", int(not closing), time.time()))

    def session(self, local_session_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE local_session_id=?",
                (local_session_id,)).fetchone()
        return dict(row) if row else None

    def sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def accept_turn(self, *, turn_id: str, local_session_id: str,
                    prompt_digest: str) -> bool:
        """Persist command receipt before starting the provider operation."""
        with self._lock, self._db:
            cursor = self._db.execute("""INSERT OR IGNORE INTO turns
                (turn_id,local_session_id,state,prompt_digest,updated_at)
                VALUES(?,?, 'accepted', ?, ?)""",
                (turn_id, local_session_id, prompt_digest, time.time()))
        return cursor.rowcount == 1

    def creation(self, request_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM session_creations WHERE request_id=?",
                                   (request_id,)).fetchone()
        return dict(row) if row else None

    def accept_creation(self, command: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("""INSERT INTO session_creations
                (request_id,generation,local_session_id,name,project_id,state,updated_at)
                VALUES(?,?,?,?,?,'accepted',?)""", tuple(command[key] for key in (
                    "request_id", "generation", "local_session_id", "name", "project_id"))
                + (time.time(),))

    def finish_creation(self, request_id: str, state: str, *, error_code: str = "",
                        hub_session_id: str = "", acknowledged: bool = False) -> None:
        with self._lock, self._db:
            self._db.execute("""UPDATE session_creations SET state=?,error_code=?,
                hub_session_id=?,acknowledged=?,updated_at=? WHERE request_id=?""",
                (state, error_code, hub_session_id, int(acknowledged), time.time(), request_id))

    def pending_creations(self, generation: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("""SELECT * FROM session_creations WHERE generation=?
                AND acknowledged=0 AND state IN ('created','failed')
                ORDER BY updated_at LIMIT 8""", (generation,)).fetchall()
        return [dict(row) for row in rows]

    def closure(self, request_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM session_closures WHERE request_id=?",
                                   (request_id,)).fetchone()
        return dict(row) if row else None

    def accept_closure(self, command: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("""INSERT INTO session_closures
                (request_id,generation,local_session_id,session_id,state,updated_at)
                VALUES(?,?,?,?,'accepted',?)""", tuple(command[key] for key in (
                    "request_id", "generation", "local_session_id", "session_id"))
                + (time.time(),))
            self._db.execute("UPDATE sessions SET status='closing',controllable=0 "
                             "WHERE local_session_id=?", (command["local_session_id"],))

    def finish_closure(self, request_id: str, state: str, *, error_code: str = "",
                       acknowledged: bool = False) -> None:
        with self._lock, self._db:
            self._db.execute("""UPDATE session_closures SET state=?,error_code=?,
                acknowledged=?,updated_at=? WHERE request_id=?""",
                (state, error_code, int(acknowledged), time.time(), request_id))
            self._db.execute("""UPDATE sessions SET status=?,controllable=0
                WHERE local_session_id=(SELECT local_session_id FROM session_closures
                                        WHERE request_id=?)""",
                ("closed" if state == "closed" else "close_failed", request_id))

    def pending_closures(self, generation: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("""SELECT * FROM session_closures WHERE generation=?
                AND acknowledged=0 AND state IN ('closed','failed')
                ORDER BY updated_at LIMIT 8""", (generation,)).fetchall()
        return [dict(row) for row in rows]

    def turn(self, turn_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        return dict(row) if row else None

    def set_turn(self, turn_id: str, state: str, runtime_turn_id: str = "") -> None:
        with self._lock, self._db:
            self._db.execute("""UPDATE turns SET state=?,runtime_turn_id=?,updated_at=?
                WHERE turn_id=?""", (state, runtime_turn_id, time.time(), turn_id))

    def enqueue_event(self, *, event_id: str, event: dict[str, Any],
                      generation: int) -> None:
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > 32 * 1024:
            raise ValueError("hub event exceeds the local outbox limit")
        with self._lock, self._db:
            self._db.execute("""INSERT OR IGNORE INTO outbox
                (event_id,body,generation,created_at) VALUES(?,?,?,?)""",
                (event_id, body, generation, time.time()))

    def pending_events(self, limit: int = 32) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("""SELECT event_id,body,generation,attempts
                FROM outbox WHERE obsolete=0 AND sent_cursor='' ORDER BY created_at
                LIMIT ?""", (max(1, min(int(limit), 128)),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["body"] = json.loads(item["body"])
            result.append(item)
        return result

    def outbox_event(self, event_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT event_id,body,generation,attempts,sent_cursor "
                                   "FROM outbox WHERE event_id=? AND obsolete=0",
                                   (event_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["body"] = json.loads(item["body"])
        return item

    def mark_event_sent(self, event_id: str, cursor: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE outbox SET sent_cursor=? WHERE event_id=?",
                             (cursor, event_id))

    def obsolete_event(self, event_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE outbox SET obsolete=1 WHERE event_id=?",
                             (event_id,))

    def note_event_failure(self, event_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE outbox SET attempts=attempts+1 WHERE event_id=?",
                             (event_id,))

    def close(self) -> None:
        with self._lock:
            self._db.close()


class StateDirectoryLock:
    """Process-lifetime exclusive lock preventing duplicate gateways per state dir."""

    def __init__(self, directory: Path):
        self.path = directory / "hub-gateway.lock"
        self._file = None

    def acquire(self) -> None:
        import fcntl

        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(self.path, 0o600)
        self._file = os.fdopen(descriptor, "a+")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            raise RuntimeError("another Gateway already uses this state directory") from exc

    def close(self) -> None:
        if self._file is None:
            return
        import fcntl

        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None


__all__ = ["GatewayStore", "StateDirectoryLock"]
