"""Explicit device lifecycle state machine for the execution fabric.

``cordoned`` and ``drain`` are operator views, not a complete lifecycle.  This
store gives the control plane a durable, fail-closed transition protocol while
keeping the old booleans in API responses for compatibility.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


LIFECYCLE_STATES = (
    "unregistered", "enrolled", "probing", "ready", "reserved", "busy",
    "cordoned", "draining", "maintenance", "offline", "stale",
    "quarantined", "retired",
)

TRANSITIONS = {
    "unregistered": {"enrolled"},
    "enrolled": {"probing", "ready", "offline", "stale", "retired"},
    "probing": {"ready", "enrolled", "offline", "stale",
                 "quarantined", "retired"},
    "ready": {"reserved", "busy", "cordoned", "draining", "offline",
              "stale", "quarantined", "retired"},
    "reserved": {"ready", "busy", "cordoned", "draining", "offline",
                  "stale", "quarantined", "retired"},
    "busy": {"ready", "reserved", "cordoned", "draining", "offline",
             "stale", "quarantined", "retired"},
    "cordoned": {"ready", "draining", "maintenance", "offline", "stale",
                 "retired"},
    "draining": {"ready", "maintenance", "offline", "stale",
                  "quarantined", "retired"},
    "maintenance": {"probing", "ready", "offline", "retired"},
    "offline": {"probing", "ready", "stale", "draining", "quarantined", "retired"},
    "stale": {"probing", "ready", "offline", "draining", "quarantined", "retired"},
    "quarantined": {"probing", "maintenance", "retired"},
    "retired": set(),
}

MAX_EVENT_BYTES = 32 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024


class LifecycleError(ValueError):
    """Raised when a lifecycle log or transition is not trustworthy."""


class DeviceLifecycleStore:
    """Append-only lifecycle log with current-state lookup and recovery."""

    def __init__(self, state_dir, filename: str = "lifecycle.jsonl") -> None:
        self.path = Path(state_dir) / filename
        self._lock = threading.RLock()
        self._states: dict[str, str] = {}
        self._events: list[dict] = []
        self._sequence = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        if self.path.is_symlink() or self.path.stat().st_size > MAX_FILE_BYTES:
            raise LifecycleError("lifecycle log path is unsafe or too large")
        with self.path.open("rb") as handle:
            for raw in iter(lambda: handle.readline(MAX_EVENT_BYTES + 1), b""):
                if len(raw) > MAX_EVENT_BYTES or not raw.endswith(b"\n"):
                    raise LifecycleError("lifecycle log contains oversized record")
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise LifecycleError("lifecycle log contains invalid JSON") from exc
                self._apply_event(event, loading=True)

    def _apply_event(self, event: dict, loading: bool = False) -> None:
        if not isinstance(event, dict):
            raise LifecycleError("lifecycle event must be an object")
        try:
            sequence = int(event.get("sequence", 0))
        except (TypeError, ValueError) as exc:
            raise LifecycleError("lifecycle sequence must be an integer") from exc
        device_id = str(event.get("device_id", "")).strip()
        old_state = str(event.get("from", ""))
        new_state = str(event.get("to", ""))
        if sequence != self._sequence + 1:
            raise LifecycleError("lifecycle sequence gap")
        if not device_id or old_state not in LIFECYCLE_STATES \
                or new_state not in LIFECYCLE_STATES:
            raise LifecycleError("invalid lifecycle event fields")
        if self._states.get(device_id, "unregistered") != old_state:
            raise LifecycleError("lifecycle event has wrong predecessor")
        if new_state not in TRANSITIONS[old_state]:
            raise LifecycleError(f"illegal lifecycle transition: {old_state} -> {new_state}")
        self._states[device_id] = new_state
        self._sequence = sequence
        self._events.append(dict(event))
        if len(self._events) > 2000:
            del self._events[:-1000]

    def current(self, device_id: str) -> str:
        return self._states.get(str(device_id), "unregistered")

    def transition(self, device_id: str, new_state: str, *, reason: str = "",
                   actor: str = "control-plane") -> str:
        """Durably perform one legal transition; same-state is idempotent."""
        device_id = str(device_id or "").strip()
        new_state = str(new_state or "").strip().lower()
        if not device_id or new_state not in LIFECYCLE_STATES:
            raise LifecycleError("device_id and valid lifecycle state required")
        with self._lock:
            return self._transition_locked(device_id, new_state, reason, actor)

    def _transition_locked(self, device_id: str, new_state: str,
                           reason: str, actor: str) -> str:
        """Apply one transition while the store lock is held."""
        old_state = self.current(device_id)
        if old_state == new_state:
            return new_state
        if new_state not in TRANSITIONS[old_state]:
            raise LifecycleError(
                f"illegal lifecycle transition: {old_state} -> {new_state}")
        event = {
            "sequence": self._sequence + 1,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "device_id": device_id, "from": old_state, "to": new_state,
            "reason": str(reason or "")[:200],
            "actor": str(actor or "control-plane")[:100],
        }
        line = json.dumps(event, ensure_ascii=False,
                          separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise LifecycleError("lifecycle event too large")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            current = self.path.stat().st_size if self.path.exists() else 0
            if current + len(encoded) > MAX_FILE_BYTES:
                raise LifecycleError("lifecycle log capacity exhausted")
            with self.path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise LifecycleError(f"cannot append lifecycle event: {exc}") from exc
        self._apply_event(event)
        return new_state

    def transition_path(self, device_id: str, states, *, reason: str = "",
                        actor: str = "control-plane") -> str:
        """Apply a legal multi-step path without interleaving another actor.

        Recovery paths such as ``stale -> probing -> ready -> reserved`` must
        be one critical section.  Otherwise the heartbeat/stale scanner can
        observe the intermediate state and append a competing transition.
        """
        device_id = str(device_id or "").strip()
        if not device_id:
            raise LifecycleError("device_id is required")
        try:
            requested = [str(state or "").strip().lower() for state in states]
        except TypeError as exc:
            raise LifecycleError("lifecycle path must be iterable") from exc
        if any(state not in LIFECYCLE_STATES for state in requested):
            raise LifecycleError("lifecycle path contains invalid state")
        with self._lock:
            result = self.current(device_id)
            candidate = result
            for state in requested:
                if state == candidate:
                    continue
                if state not in TRANSITIONS[candidate]:
                    raise LifecycleError(
                        f"illegal lifecycle transition: {candidate} -> {state}")
                candidate = state
            for state in requested:
                result = self._transition_locked(device_id, state, reason, actor)
            return result

    def recent(self, limit: int = 50, device_id: str = "") -> list[dict]:
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 50
        events = [event for event in self._events
                  if not device_id or event.get("device_id") == device_id]
        return [dict(event) for event in events[-limit:]]
