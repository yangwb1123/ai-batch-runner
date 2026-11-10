"""Bounded immutable file payloads sharing the Gateway's private SQLite owner."""

from __future__ import annotations

import hashlib
import json
import time

from .hub_file_snapshot_models import canonical_bytes, validate_upload

MAX_PENDING = 16
MAX_PENDING_BYTES = 8 * 1024 * 1024
MAX_RECEIPTS = 4096


class GatewayFileQueue:
    def __init__(self, store):
        self.store = store
        with store._lock, store._db:
            store._db.execute("""CREATE TABLE IF NOT EXISTS file_snapshots (
                turn_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                snapshot_set_id TEXT NOT NULL, terminal_event_id TEXT NOT NULL,
                state TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
                payload_digest TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
            )""")

    def reserve(self, body, terminal_event_id):
        """Own a turn before reading; existing receipts never trigger recapture."""
        with self.store._lock, self.store._db:
            if self.receipt(body['turn_id']) is not None:
                return False
            counts = self.store._db.execute("""SELECT COUNT(*),
                SUM(CASE WHEN state IN ('capturing','pending') THEN 1 ELSE 0 END)
                FROM file_snapshots""").fetchone()
            if counts[0] >= MAX_RECEIPTS:
                return False
            available = (counts[1] or 0) < MAX_PENDING
            self.store._db.execute("""INSERT INTO file_snapshots
                (turn_id,generation,snapshot_set_id,terminal_event_id,state,error_code,created_at)
                VALUES(?,?,?,?,?,?,?)""", (body['turn_id'], body['generation'],
                body['snapshot_set_id'], terminal_event_id,
                'capturing' if available else 'rejected',
                '' if available else 'capacity_exceeded', time.time()))
            return available

    def save(self, body):
        encoded = canonical_bytes(validate_upload(body))
        digest = hashlib.sha256(encoded).hexdigest()
        with self.store._lock, self.store._db:
            row = self.receipt(body['turn_id'])
            if (row is None or row['snapshot_set_id'] != body['snapshot_set_id']
                    or row['generation'] != body['generation']):
                raise ValueError('file snapshot reservation is immutable')
            if row['payload_digest']:
                if row['payload_digest'] != digest:
                    raise ValueError('file snapshot payload is immutable')
                return
            if row['state'] != 'capturing':
                raise ValueError('file snapshot reservation is unavailable')
            size = self.store._db.execute(
                "SELECT COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM file_snapshots"
            ).fetchone()[0]
            if size + len(encoded) > MAX_PENDING_BYTES:
                self.finish(body['turn_id'], 'rejected', 'capacity_exceeded')
                return
            self.store._db.execute("""UPDATE file_snapshots SET body=?,payload_digest=?,
                state='pending' WHERE turn_id=?""", (encoded.decode('utf-8'), digest, body['turn_id']))

    def pending(self, generation):
        with self.store._lock:
            rows = self.store._db.execute("""SELECT f.* FROM file_snapshots f JOIN outbox e
                ON e.event_id=f.terminal_event_id AND e.generation=f.generation
                WHERE f.state='pending' AND f.generation=? AND e.sent_cursor!=''
                AND e.obsolete=0 ORDER BY f.created_at LIMIT 1""", (generation,)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value['body'] = validate_upload(json.loads(value['body']))
            if hashlib.sha256(canonical_bytes(value['body'])).hexdigest() != row['payload_digest']:
                raise ValueError('stored file snapshot integrity failed')
            result.append(value)
        return result

    def receipt(self, turn_id):
        with self.store._lock:
            row = self.store._db.execute('SELECT * FROM file_snapshots WHERE turn_id=?',
                                         (turn_id,)).fetchone()
        return dict(row) if row else None

    def failure(self, turn_id, code):
        with self.store._lock, self.store._db:
            self.store._db.execute("""UPDATE file_snapshots SET attempts=MIN(attempts+1,999999),
                error_code=? WHERE turn_id=? AND state='pending'""", (code, turn_id))

    def finish(self, turn_id, state, code=''):
        if state not in {'acknowledged', 'rejected', 'obsolete'}:
            raise ValueError('invalid snapshot receipt state')
        with self.store._lock, self.store._db:
            self.store._db.execute("""UPDATE file_snapshots SET state=?,error_code=?,body=''
                WHERE turn_id=?""", (state, code, turn_id))

    def activate_generation(self, generation):
        with self.store._lock, self.store._db:
            self.store._db.execute("""UPDATE file_snapshots SET state='obsolete',body='',
                error_code='generation_changed' WHERE generation!=?
                AND state IN ('pending','capturing')""", (generation,))
