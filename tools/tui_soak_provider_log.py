"""Atomic fixture evidence: committed SQLite rows and a harness-owned JSONL export."""

import json
from pathlib import Path
import sqlite3

from tui_soak_store_guard import connection


MAX_BYTES, MAX_RECORDS, LOCK_TIMEOUT = 1024 * 1024, 128, 3.0
SCHEMA = "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, payload BLOB NOT NULL)"


def database_path(path: Path) -> Path:
    return path.with_suffix(".sqlite")


def append_provider_event(path: Path, value: dict) -> None:
    """A killed writer publishes either its entire record or no record."""
    raw = (json.dumps(value) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("oversized provider evidence record")
    with connection(database_path(path), LOCK_TIMEOUT) as store:
        store.execute("BEGIN IMMEDIATE")
        store.execute(SCHEMA)
        store.execute("INSERT INTO events (payload) VALUES (?)", (raw,))
        store.commit()


class ProviderLog:
    def __init__(self, path: Path, failure):
        self.path, self.failure = path, failure
        self.offset, self.exhausted = 0, False
        try:
            with connection(database_path(path), LOCK_TIMEOUT) as store:
                store.execute(SCHEMA)
                store.commit()
        except sqlite3.Error as exc:
            raise failure(f"cannot initialize provider evidence store: {exc}") from exc

    def read(self) -> list[dict]:
        try:
            with connection(database_path(self.path), LOCK_TIMEOUT) as store:
                rows = self._batch(store)
        except sqlite3.Error as exc:
            raise self.failure(f"cannot read provider evidence store: {exc}") from exc
        self.exhausted = not rows
        if not rows:
            return []
        # Only the harness writes this export; fixture processes never append
        # here. A failed export aborts acceptance and never advances its cursor.
        with self.path.open("ab") as output:
            output.write(b"".join(raw for _identity, raw, _value in rows))
        self.offset = rows[-1][0]
        return [value for _identity, _raw, value in rows]

    def _batch(self, connection) -> list[tuple]:
        rows, total = [], 0
        cursor = connection.execute(
            "SELECT id, length(payload), substr(payload, 1, ?) FROM events WHERE id>? ORDER BY id LIMIT ?",
            (MAX_BYTES + 1, self.offset, MAX_RECORDS))
        for identity, size, raw in cursor:
            if size > MAX_BYTES or not isinstance(raw, bytes):
                raise self.failure("oversized or non-byte provider evidence record")
            if identity != self.offset + len(rows) + 1:
                raise self.failure("provider evidence sequence has a missing committed record")
            if rows and total + size > MAX_BYTES:
                break
            value = self._decode(raw)
            rows.append((identity, raw, value))
            total += size
        return rows

    def _decode(self, raw: bytes) -> dict:
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise self.failure("invalid committed provider evidence JSON") from exc
        if not isinstance(value, dict) or not raw.endswith(b"\n"):
            raise self.failure("incomplete or non-object committed provider evidence")
        return value
