"""Bounded, disposable projection checkpoint metadata.

A checkpoint is a cache and an integrity witness for a trusted journal
boundary.  It is never used as an execution grant, a provider command, or a
replacement for journal replay.  Invalid, stale, or missing checkpoints are
ignored and the durable event stream remains authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import hashlib
import json
import os
from types import MappingProxyType
from typing import Any, Mapping, Optional


CHECKPOINT_SCHEMA_VERSION = 1
MAX_CHECKPOINT_BYTES = 16 * 1024
MAX_CHECKPOINT_ID_BYTES = 256
MAX_CHECKPOINT_DIGEST_NODES = 4_096
_CHECKPOINT_FIELDS = frozenset({
    "schema_version", "scope", "session_id", "journal_bytes",
    "last_event_id", "last_event_seq", "projection_revision",
    "projection_digest",
})


@dataclass(frozen=True)
class ProjectionCheckpoint:
    """Immutable metadata for one exact journal/projection boundary."""

    scope: Mapping[str, Any]
    session_id: str
    journal_bytes: int
    last_event_id: str
    last_event_seq: int
    projection_revision: int
    projection_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", MappingProxyType(dict(self.scope)))

    def mapping(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "scope": dict(self.scope),
            "session_id": self.session_id,
            "journal_bytes": self.journal_bytes,
            "last_event_id": self.last_event_id,
            "last_event_seq": self.last_event_seq,
            "projection_revision": self.projection_revision,
            "projection_digest": self.projection_digest,
        }


def projection_digest(snapshot: Any) -> str:
    """Hash bounded durable projection fields without storing their contents."""
    value = _durable_snapshot(snapshot)
    encoded = json.dumps(_normalize(value, [0]), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_projection_checkpoint(journal: Any, event: Any,
                                snapshot: Any) -> ProjectionCheckpoint:
    """Create a checkpoint from already committed journal/projection state."""
    if getattr(event, "session_id", None) != journal.session_id:
        raise ValueError("checkpoint event does not match journal session")
    event_id = _id(getattr(event, "event_id", ""), "event_id")
    event_seq = _nonnegative(getattr(event, "seq", 0), "event_seq")
    revision = _nonnegative(getattr(snapshot, "revision", 0), "revision")
    size = _nonnegative(journal.recovery.total_bytes, "journal_bytes")
    return ProjectionCheckpoint(
        scope=journal.scope, session_id=journal.session_id,
        journal_bytes=size, last_event_id=event_id,
        last_event_seq=event_seq, projection_revision=revision,
        projection_digest=projection_digest(snapshot))


def write_projection_checkpoint(journal: Any,
                                checkpoint: ProjectionCheckpoint) -> bool:
    """Best-effort CAS write; checkpoint failures never fail execution."""
    value = _checkpoint_mapping(checkpoint)
    journal._validate_current_boundary()
    with journal._thread_lock, journal._locked():
        journal._ensure_meta()
        descriptor = journal._open_journal(create=False)
        if descriptor is None:
            return False
        try:
            info = os.fstat(descriptor)
            if info.st_size != checkpoint.journal_bytes:
                return False
            events = journal.recovery.events
            if not events or events[-1].event_id != checkpoint.last_event_id:
                return False
        finally:
            os.close(descriptor)
        from .tui_journal import _atomic_json_best_effort
        return _atomic_json_best_effort(journal.checkpoint_path, value)


def read_projection_checkpoint(journal: Any, expected_bytes: int
                               ) -> tuple[Optional[ProjectionCheckpoint], str]:
    """Read a checkpoint only when its scope and exact journal boundary match."""
    path = journal.checkpoint_path
    if not path.exists() and not path.is_symlink():
        return None, "missing"
    if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool):
        return None, "invalid"
    try:
        journal._validate_current_boundary()
        with journal._thread_lock, journal._locked():
            descriptor = journal._open_journal(create=False)
            if descriptor is None:
                return None, "stale"
            try:
                if os.fstat(descriptor).st_size != expected_bytes:
                    return None, "stale"
                from .tui_journal import _read_json
                value = _read_json(path)
            finally:
                os.close(descriptor)
        checkpoint = _parse_checkpoint(journal, value)
        if checkpoint.journal_bytes != expected_bytes:
            return None, "stale"
        events = journal.recovery.events
        if (not events or events[-1].event_id != checkpoint.last_event_id
                or events[-1].seq != checkpoint.last_event_seq):
            return None, "stale"
        return checkpoint, ""
    except (OSError, TypeError, ValueError, RuntimeError):
        return None, "invalid"


def verify_projection_checkpoint(journal: Any, snapshot: Any,
                                 expected_bytes: int
                                 ) -> tuple[Optional[ProjectionCheckpoint], str]:
    """Verify a cache after replay without allowing it to affect projection."""
    checkpoint, status = read_projection_checkpoint(journal, expected_bytes)
    if checkpoint is None:
        return None, status
    if checkpoint.projection_digest != projection_digest(snapshot):
        return checkpoint, "mismatch"
    return checkpoint, "verified"


def _checkpoint_mapping(value: ProjectionCheckpoint) -> dict[str, Any]:
    if not isinstance(value, ProjectionCheckpoint):
        raise TypeError("checkpoint must be a ProjectionCheckpoint")
    mapping = value.mapping()
    encoded = json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
        raise ValueError("projection checkpoint exceeds size limit")
    return mapping


def _parse_checkpoint(journal: Any, value: Mapping[str, Any]
                      ) -> ProjectionCheckpoint:
    if not isinstance(value, Mapping) or set(value) != _CHECKPOINT_FIELDS:
        raise ValueError("projection checkpoint schema mismatch")
    if value.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("projection checkpoint version mismatch")
    if value.get("scope") != journal.scope or value.get("session_id") != journal.session_id:
        raise ValueError("projection checkpoint scope mismatch")
    return ProjectionCheckpoint(
        scope=journal.scope, session_id=journal.session_id,
        journal_bytes=_nonnegative(value.get("journal_bytes"), "journal_bytes"),
        last_event_id=_id(value.get("last_event_id"), "last_event_id"),
        last_event_seq=_nonnegative(value.get("last_event_seq"), "last_event_seq"),
        projection_revision=_nonnegative(
            value.get("projection_revision"), "projection_revision"),
        projection_digest=_digest(value.get("projection_digest")))


def _durable_snapshot(snapshot: Any) -> dict[str, Any]:
    excluded = {
        "revision", "assistant_text", "error_text", "assistant_text_start",
        "error_text_start", "assistant_generation", "recovery",
    }
    if not is_dataclass(snapshot):
        return {"snapshot": snapshot}
    return {
        item.name: getattr(snapshot, item.name)
        for item in fields(snapshot) if item.name not in excluded
    }


def _normalize(value: Any, budget: list[int], depth: int = 0) -> Any:
    if budget[0] >= MAX_CHECKPOINT_DIGEST_NODES or depth > 16:
        return "[checkpoint-bound]"
    budget[0] += 1
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")
        return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if is_dataclass(value):
        return _normalize({item.name: getattr(value, item.name)
                           for item in fields(value)}, budget, depth + 1)
    if isinstance(value, Mapping):
        pairs = sorted(((str(key), item) for key, item in value.items()),
                       key=lambda pair: pair[0])[:256]
        result = {key: _normalize(item, budget, depth + 1)
                  for key, item in pairs}
        if len(value) > len(pairs):
            result["[items-truncated]"] = len(value)
        return result
    if isinstance(value, (tuple, list)):
        items = [_normalize(item, budget, depth + 1) for item in value[:256]]
        if len(value) > len(items):
            items.append({"[items-truncated]": len(value)})
        return items
    return {"type": type(value).__name__}


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value:
        raise ValueError(f"checkpoint {label} is invalid")
    if len(value.encode("utf-8")) > MAX_CHECKPOINT_ID_BYTES:
        raise ValueError(f"checkpoint {label} is too long")
    return value


def _nonnegative(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"checkpoint {label} is invalid")
    return value


def _digest(value: Any) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError("checkpoint projection digest is invalid")
    return value
