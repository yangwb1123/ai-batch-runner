"""Disposable, bounded session-index summaries derived from trusted journals.

The sidecar accelerates the picker only.  Resume and fork always recover the
event journal again; no field here grants an executable capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Optional

from .tui_protocol import MAX_EVENT_LINE_BYTES, RuntimeEvent
from .tui_view_support import clean_text, one_line


SESSION_SUMMARY_SCHEMA_VERSION = 2
MAX_SUMMARY_PREVIEW_BYTES = 512
MAX_SUMMARY_TEXT_BYTES = 2_048
_SESSION_EVENT_KINDS = frozenset({
    "session.created", "session.resumed", "session.forked",
})
_SUMMARY_FIELDS = frozenset({
    "schema_version", "scope", "session_id", "cwd_id", "journal_bytes",
    "last_event_id", "last_event_at", "workspace_id", "runtime",
    "provider_session_id", "model", "branch", "turn_state",
    "requires_reconciliation", "read_only", "preview", "preview_truncated",
})


@dataclass(frozen=True)
class TUISessionSummary:
    """Small local display projection bound to an exact journal byte size."""

    journal_bytes: int = 0
    last_event_id: str = ""
    last_event_at: str = ""
    workspace_id: str = ""
    runtime: str = ""
    provider_session_id: str = ""
    model: str = ""
    branch: str = ""
    turn_state: str = ""
    requires_reconciliation: bool = False
    read_only: bool = False
    preview: str = ""
    preview_truncated: bool = False


def build_session_summary(
        events: Iterable[RuntimeEvent], *, journal_bytes: int,
        turn_state: str, requires_reconciliation: bool,
        read_only: bool) -> TUISessionSummary:
    """Fold already-persisted (therefore redacted) events into a display cache."""
    summary = TUISessionSummary()
    for event in events:
        summary = _fold_event(summary, event)
    return _with_projection(
        summary, journal_bytes, turn_state,
        requires_reconciliation, read_only)


def advance_session_summary(
        summary: Optional[TUISessionSummary], event: RuntimeEvent, *,
        journal_bytes: int, turn_state: str,
        requires_reconciliation: bool, read_only: bool) -> TUISessionSummary:
    """Advance a live cache after the corresponding projection commit."""
    updated = _fold_event(summary or TUISessionSummary(), event)
    return _with_projection(
        updated, journal_bytes, turn_state,
        requires_reconciliation, read_only)


def summary_mapping(
        journal: Any, summary: TUISessionSummary) -> dict[str, Any]:
    """Bind a cache payload to immutable journal metadata."""
    return {
        "schema_version": SESSION_SUMMARY_SCHEMA_VERSION,
        "scope": dict(journal.scope),
        "session_id": journal.session_id,
        "cwd_id": _digest(journal.cwd),
        **summary.__dict__,
    }


def read_session_summary(
        journal: Any, journal_bytes: int
        ) -> tuple[Optional[TUISessionSummary], str]:
    """Read one sidecar at a locked journal boundary without replaying history."""
    path = journal.summary_path
    if not path.exists() and not path.is_symlink():
        return None, "missing"
    if (isinstance(journal_bytes, bool) or not isinstance(journal_bytes, int)
            or journal_bytes < 0):
        return None, "invalid"
    try:
        journal._validate_current_boundary()
        with journal._thread_lock, journal._locked():
            descriptor = journal._open_journal(create=False)
            if descriptor is None:
                return None, "stale"
            try:
                before = os.fstat(descriptor)
                if before.st_size != journal_bytes:
                    return None, "stale"
                value = _read_summary_cache(journal)
                summary = _parse_summary(journal, value)
                if summary.journal_bytes != before.st_size:
                    return None, "stale"
                if summary.last_event_id != _tail_event_id(
                        descriptor, before.st_size):
                    return None, "stale"
                after = os.fstat(descriptor)
                if (after.st_size != before.st_size
                        or after.st_mtime_ns != before.st_mtime_ns
                        or not _descriptor_matches_path(journal, descriptor)):
                    return None, "stale"
            finally:
                os.close(descriptor)
    except (OSError, RecursionError, TypeError, ValueError):
        return None, "invalid"
    return summary, ""


def write_session_summary_cache(
        journal: Any, summary: TUISessionSummary, *, expected_bytes: int) -> bool:
    """Best-effort CAS write; failure never reclassifies a durable event."""
    from .tui_journal import _atomic_json_best_effort

    value = summary_mapping(journal, summary)
    if (isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int)
            or expected_bytes < 0
            or value.get("journal_bytes") != expected_bytes):
        raise ValueError("session summary cache has an invalid journal boundary")
    journal._validate_current_boundary()
    with journal._thread_lock, journal._locked():
        journal._ensure_meta()
        descriptor = journal._open_journal(create=False)
        if descriptor is None:
            return False
        try:
            info = os.fstat(descriptor)
            if (info.st_size != expected_bytes
                    or summary.last_event_id != _tail_event_id(
                        descriptor, expected_bytes)
                    or not _descriptor_matches_path(journal, descriptor)):
                return False
        finally:
            os.close(descriptor)
        return _atomic_json_best_effort(journal.summary_path, value)


def _read_summary_cache(journal: Any) -> dict:
    from .tui_journal import _read_json

    journal._validate_current_boundary()
    return _read_json(journal.summary_path)


def _fold_event(summary: TUISessionSummary, event: RuntimeEvent) -> TUISessionSummary:
    values: dict[str, Any] = {
        "last_event_id": event.event_id,
        "last_event_at": _bounded_text(event.occurred_at, MAX_SUMMARY_TEXT_BYTES),
        "workspace_id": _bounded_text(event.workspace_id, MAX_SUMMARY_TEXT_BYTES),
    }
    if event.kind in _SESSION_EVENT_KINDS:
        values.update(provider_session_id="", runtime="", model="")
        provider_id = event.payload.get("provider_session_id")
        runtime = event.extensions.get("runtime")
        if _usable_provider_id(provider_id) and _usable_runtime(runtime):
            values["provider_session_id"] = str(provider_id)
            values["runtime"] = _bounded_text(runtime, MAX_SUMMARY_TEXT_BYTES)
            model = event.extensions.get("model")
            if isinstance(model, str):
                values["model"] = _bounded_text(model, 512)
    if event.kind == "workspace.snapshot" and event.extensions.get("authoritative") is True:
        workspace = event.payload.get("workspace", event.payload)
        if isinstance(workspace, Mapping) and isinstance(workspace.get("branch"), str):
            values["branch"] = _bounded_text(workspace["branch"], 512)
    if event.kind == "message.completed":
        preview, truncated = _preview(event.payload.get(
            "text", event.payload.get("message", "")))
        values["preview"] = preview
        values["preview_truncated"] = bool(
            truncated or event.payload.get("truncated") is True
            or event.extensions.get("journal_payload_truncated") is True)
    return replace(summary, **values)


def _with_projection(
        summary: TUISessionSummary, journal_bytes: int,
        turn_state: str, requires_reconciliation: bool,
        read_only: bool) -> TUISessionSummary:
    if (isinstance(journal_bytes, bool) or not isinstance(journal_bytes, int)
            or journal_bytes < 0):
        raise ValueError("journal_bytes must be a non-negative integer")
    return replace(
        summary,
        journal_bytes=journal_bytes,
        turn_state=_bounded_text(turn_state, 128),
        requires_reconciliation=bool(requires_reconciliation),
        read_only=bool(read_only),
    )


def _parse_summary(journal: Any, value: Mapping[str, Any]) -> TUISessionSummary:
    if not isinstance(value, Mapping) or set(value) != _SUMMARY_FIELDS:
        raise ValueError("session summary schema mismatch")
    expected = {
        "schema_version": SESSION_SUMMARY_SCHEMA_VERSION,
        "scope": journal.scope,
        "session_id": journal.session_id,
        "cwd_id": _digest(journal.cwd),
    }
    if any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("session summary boundary mismatch")
    journal_bytes = value.get("journal_bytes")
    if (isinstance(journal_bytes, bool) or not isinstance(journal_bytes, int)
            or journal_bytes < 0):
        raise ValueError("session summary journal_bytes is invalid")
    text_limits = {
        "last_event_id": MAX_SUMMARY_TEXT_BYTES,
        "last_event_at": MAX_SUMMARY_TEXT_BYTES,
        "workspace_id": MAX_SUMMARY_TEXT_BYTES,
        "runtime": MAX_SUMMARY_TEXT_BYTES,
        "provider_session_id": MAX_SUMMARY_TEXT_BYTES,
        "model": 512,
        "branch": 512,
        "turn_state": 128,
        "preview": MAX_SUMMARY_PREVIEW_BYTES,
    }
    texts = {key: _validated_text(value.get(key), key, limit)
             for key, limit in text_limits.items()}
    provider_id = texts["provider_session_id"]
    if provider_id and not _usable_provider_id(provider_id):
        raise ValueError("session summary provider id is invalid")
    booleans = {}
    for key in ("requires_reconciliation", "read_only", "preview_truncated"):
        item = value.get(key)
        if not isinstance(item, bool):
            raise ValueError(f"session summary {key} is invalid")
        booleans[key] = item
    return TUISessionSummary(journal_bytes=journal_bytes, **texts, **booleans)


def _preview(value: Any) -> tuple[str, bool]:
    text = one_line(clean_text(value))
    return _bounded_text_with_flag(text, MAX_SUMMARY_PREVIEW_BYTES)


def _bounded_text(value: Any, maximum: int) -> str:
    return _bounded_text_with_flag(value, maximum)[0]


def _bounded_text_with_flag(value: Any, maximum: int) -> tuple[str, bool]:
    text = one_line(clean_text(value))
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return text, False
    marker = " …[truncated]"
    room = max(0, maximum - len(marker.encode()))
    prefix = encoded[:room].decode("utf-8", errors="ignore")
    return prefix + marker, True


def _validated_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"session summary {label} is invalid")
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) > maximum or value != one_line(clean_text(value)):
        raise ValueError(f"session summary {label} is unsafe")
    return value


def _usable_provider_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if any(marker in value for marker in ("[REDACTED]", "...[truncated]")):
        return False
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return False
    return "\n" not in value and "\r" not in value and size <= 2_048


def _usable_runtime(value: Any) -> bool:
    if not isinstance(value, str) or not value or value.strip() != value:
        return False
    if any(marker in value for marker in ("[REDACTED]", "...[truncated]")):
        return False
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return False
    return size <= 128 and value == one_line(clean_text(value))


def _tail_event_id(descriptor: int, size: int) -> str:
    if size <= 0:
        return ""
    window = min(size, MAX_EVENT_LINE_BYTES)
    raw = os.pread(descriptor, window, size - window)
    if len(raw) != window or not raw.endswith(b"\n"):
        raise ValueError("journal tail is incomplete")
    body = raw[:-1]
    boundary = body.rfind(b"\n")
    line = body[boundary + 1:] + b"\n"
    if not line or len(line) > MAX_EVENT_LINE_BYTES:
        raise ValueError("journal tail exceeds the event bound")
    try:
        event = RuntimeEvent.from_jsonl(line.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("journal tail is not a runtime event") from exc
    return event.event_id


def _descriptor_matches_path(journal: Any, descriptor: int) -> bool:
    try:
        opened = os.fstat(descriptor)
        current = os.stat(journal.path, follow_symlinks=False)
    except OSError:
        return False
    return bool(
        stat.S_ISREG(current.st_mode)
        and opened.st_nlink == 1
        and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino))


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]
