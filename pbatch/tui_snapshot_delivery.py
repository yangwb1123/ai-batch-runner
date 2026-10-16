"""Coalesce folded runtime snapshots behind one bounded UI notice.

Protocol events, ledger facts and required folding stay synchronous in the
consumer.  Only presentation is deferred: at most one notice is in flight and
the UI applies the newest immutable snapshot when it reaches that notice.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from textual.message import Message


class RuntimeSnapshotReady(Message, bubble=False):
    """A queued notice that the delivery holds a newer immutable view."""

    def __init__(self, delivery_id: int, ticket: int) -> None:
        super().__init__()
        self.delivery_id = int(delivery_id)
        self.ticket = int(ticket)


class SnapshotDelivery:
    """Keep the latest immutable view behind at most one pending notice.

    ``identity`` is the exact controller object whose snapshots this delivery
    is allowed to present.  ``reidentify`` invalidates every queued notice when
    the active controller is replaced, and ``close`` stops delivery for good.
    """

    def __init__(self, post: Callable[[RuntimeSnapshotReady], bool],
                 identity: Any) -> None:
        self._post = post
        self._identity = identity
        self._lock = threading.Lock()
        self._latest: Optional[Any] = None
        self._taken = -1
        self._pending = 0
        self._next_ticket = 0
        self._delivery_id = 0
        self._closed = False

    def reidentify(self, identity: Any) -> int:
        """Invalidate queued notices and bind delivery to a new controller."""
        with self._lock:
            self._identity = identity
            self._latest = None
            self._taken = -1
            self._pending = 0
            self._delivery_id += 1
            return self._delivery_id

    def close(self) -> None:
        """Stop delivering; queued notices can no longer change presentation."""
        with self._lock:
            self._closed = True
            self._latest = None
            self._taken = -1
            self._pending = 0
            self._delivery_id += 1

    def offer(self, identity: Any, view: Any) -> bool:
        """Store the newest view and post at most one notice."""
        if view is None:
            return False
        with self._lock:
            if self._closed or identity != self._identity:
                return False
            if view.revision <= self._taken:
                return False
            current = self._latest
            if current is not None and view.revision <= current.revision:
                return False
            self._latest = view
            if self._pending:
                return True
            self._next_ticket += 1
            self._pending = self._next_ticket
            ticket, delivery_id = self._pending, self._delivery_id
        accepted = True
        try:
            accepted = self._post(RuntimeSnapshotReady(delivery_id, ticket)) is not False
        except (AttributeError, RuntimeError, TypeError):
            accepted = False
        if not accepted:
            with self._lock:
                if self._pending == ticket:
                    self._pending = 0
        return True

    def take(self, delivery_id: int, ticket: int) -> Optional[Any]:
        """Consume exactly this notice's newest view; stale tickets are no-ops."""
        with self._lock:
            if (self._closed or delivery_id != self._delivery_id
                    or ticket != self._pending):
                return None
            self._pending = 0
            view, self._latest = self._latest, None
            if view is not None:
                self._taken = max(self._taken, view.revision)
            return view
