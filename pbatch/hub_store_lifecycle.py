"""Keep the cooperative Hub writer lock until every admitted SQLite connection closes."""
from __future__ import annotations

import sqlite3
import threading

from .hub_store_helpers import HubStoreError


class OwnedConnection(sqlite3.Connection):
    """A private Hub connection; its context manager also closes the handle."""

    def close(self):
        # A failed close (for example, from the wrong thread) must NOT release ownership.
        super().close()
        finish = getattr(self, '_finish', None)
        if finish is not None:
            self._finish = None
            finish()

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class StoreConnections:
    def __init__(self, release_owner, validate_owner):
        self._release_owner = release_owner
        self._validate_owner = validate_owner
        self._mutex = threading.Lock()
        self._active = 0
        self._closed = False

    def connect(self, path, *, readonly=False):
        with self._mutex:
            if self._closed:
                raise HubStoreError('store_closed', 'HubStore is closed', 503)
            self._active += 1
        try:
            self._validate_owner()
            target = path.as_uri() + ('?mode=ro' if readonly else '?mode=rw')
            connection = sqlite3.connect(target, uri=True, timeout=5.0, isolation_level=None,
                                         factory=OwnedConnection)
        except BaseException:
            self._finish()
            raise
        connection._finish = self._finish
        try:
            self._validate_owner()
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute('PRAGMA busy_timeout = 5000')
            connection.execute('PRAGMA synchronous = FULL')
        except BaseException:
            connection.close()
            raise
        return connection

    def _finish(self):
        with self._mutex:
            self._active -= 1
            if self._closed and not self._active:
                self._release_owner()

    def require_drained(self):
        with self._mutex:
            if not self._closed or self._active:
                raise HubStoreError('store_draining', 'HubStore closure is not complete', 503)

    def close(self):
        """Refuse new work immediately; retain the lock for already admitted work."""
        with self._mutex:
            self._closed = True
            if not self._active:
                self._release_owner()
