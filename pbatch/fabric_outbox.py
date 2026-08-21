"""Bounded, restart-safe Runner evidence outbox.

The outbox is deliberately local to one device identity.  It buffers only
transport failures; the control plane remains the authority for fencing and
may permanently reject a stale lease when the task was migrated or lost.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PENDING = 256


class EvidenceOutboxError(ValueError):
    """Raised when an outbox record cannot be trusted or safely persisted."""


class EvidenceOutbox:
    """A bounded JSONL queue with replay and acknowledgement semantics."""

    def __init__(self, state_dir, filename: str = "evidence-outbox.jsonl") -> None:
        self.path = Path(state_dir) / filename
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self._load()

    @staticmethod
    def _key(record: dict) -> str:
        task_id = str(record.get("task_id", ""))
        lease_id = str(record.get("lease_id", ""))
        attempt_id = str(record.get("attempt_id", ""))
        if not task_id or not lease_id or not attempt_id:
            raise EvidenceOutboxError("outbox record missing task/lease/attempt")
        return f"{task_id}:{lease_id}:{attempt_id}"

    @staticmethod
    def _line(event: dict) -> bytes:
        line = (json.dumps(event, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > MAX_RECORD_BYTES:
            raise EvidenceOutboxError("outbox record too large")
        return line

    def _load(self) -> None:
        if not self.path.exists():
            return
        if self.path.is_symlink() or self.path.stat().st_size > MAX_FILE_BYTES:
            raise EvidenceOutboxError("outbox path is unsafe or too large")
        with self.path.open("rb") as handle:
            for raw in iter(lambda: handle.readline(MAX_RECORD_BYTES + 1), b""):
                if len(raw) > MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                    raise EvidenceOutboxError("outbox contains oversized record")
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise EvidenceOutboxError("outbox contains invalid JSON") from exc
                self._apply(event)
        if len(self._entries) > MAX_PENDING:
            raise EvidenceOutboxError("outbox contains too many pending records")

    def _apply(self, event: dict) -> None:
        if not isinstance(event, dict):
            raise EvidenceOutboxError("outbox event must be an object")
        key = str(event.get("key", ""))
        status = str(event.get("status", ""))
        if not key or status not in ("pending", "delivered", "rejected"):
            raise EvidenceOutboxError("invalid outbox event")
        if status == "pending":
            payload = event.get("payload")
            if not isinstance(payload, dict) or self._key(payload) != key:
                raise EvidenceOutboxError("invalid pending outbox payload")
            self._entries[key] = dict(payload)
        else:
            self._entries.pop(key, None)

    def _compact_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with temp.open("wb") as handle:
                for key, payload in self._entries.items():
                    handle.write(self._line({"key": key, "status": "pending",
                                             "payload": payload}))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            try:
                temp.unlink()
            except OSError:
                pass
            raise EvidenceOutboxError(f"cannot compact outbox: {exc}") from exc

    def _append_locked(self, event: dict) -> None:
        line = self._line(event)
        current = self.path.stat().st_size if self.path.exists() else 0
        if current + len(line) > MAX_FILE_BYTES:
            self._compact_locked()
            current = self.path.stat().st_size if self.path.exists() else 0
        if current + len(line) > MAX_FILE_BYTES:
            raise EvidenceOutboxError("outbox file capacity exhausted")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise EvidenceOutboxError(f"cannot append outbox: {exc}") from exc

    def enqueue(self, record: dict) -> str:
        """Persist one evidence submission; identical enqueue is idempotent."""
        if not isinstance(record, dict):
            raise EvidenceOutboxError("outbox record must be an object")
        key = self._key(record)
        payload = dict(record)
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                if existing != payload:
                    raise EvidenceOutboxError("conflicting outbox record")
                return key
            if len(self._entries) >= MAX_PENDING:
                raise EvidenceOutboxError("outbox pending capacity exhausted")
            self._append_locked({"key": key, "status": "pending",
                                 "payload": payload})
            self._entries[key] = payload
        return key

    def acknowledge(self, key: str, status: str = "delivered",
                    reason: str = "") -> bool:
        """Remove a pending item after an authoritative result."""
        if status not in ("delivered", "rejected"):
            raise EvidenceOutboxError("invalid outbox acknowledgement")
        with self._lock:
            if key not in self._entries:
                return False
            self._append_locked({"key": key, "status": status,
                                 "reason": str(reason)[:200]})
            self._entries.pop(key, None)
            return True

    def pending(self, limit: int = MAX_PENDING) -> list[dict]:
        """Return a bounded snapshot for one flush pass."""
        try:
            limit = max(1, min(int(limit), MAX_PENDING))
        except (TypeError, ValueError):
            limit = MAX_PENDING
        with self._lock:
            return [dict(record) for record in list(self._entries.values())[:limit]]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
