"""Crash-safe, scoped RuntimeEvent journal for disposable TUI projections."""

from __future__ import annotations

import base64
import contextlib
import errno
from . import file_lock as fcntl
import hashlib
import json
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config, tui_journal_archive
from .memory_policy import memory_scope, scope_matches
from .memory_records import redact, redact_content
from .tui_journal_plan import (
    attach_plan_projection_identities, plan_projection_payload,
    preserve_actor_lineage,
)
from .tui_protocol import (
    MAX_EVENT_LINE_BYTES, RUNTIME_IDENTITY_FIELDS, RuntimeEvent,
)

JOURNAL_SCHEMA_VERSION = 1
MAX_JOURNAL_META_BYTES = 32 * 1024
MAX_JOURNAL_ID_BYTES = 2 * 1024
MAX_JOURNAL_RUNS = 4_096


class RuntimeEventJournalError(ValueError):
    """Raised when a journal cannot be trusted or safely extended."""

    def __init__(self, message: str, *, retryable: bool = False,
                 journal_fatal: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.journal_fatal = journal_fatal


@dataclass(frozen=True)
class JournalRecovery:
    """Bounded trusted prefix recovered from one session journal."""

    events: tuple[RuntimeEvent, ...] = ()
    warnings: tuple[str, ...] = ()
    truncated_tail: bool = False
    degraded: bool = False
    trusted_bytes: int = 0
    total_bytes: int = 0

    @property
    def writable(self) -> bool:
        return not self.degraded


@dataclass(frozen=True)
class _RawRecord:
    start: int
    end: int
    raw: bytes
    complete: bool
    oversized: bool


class RuntimeEventJournal:
    """One append-only, fsynced event stream for a logical TUI session."""

    def __init__(self, cwd: str, session_id: str, *,
                 max_bytes: Optional[int] = None) -> None:
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        self.session_id = _bounded_id(session_id, "session_id")
        self.max_bytes = (config.TUI_JOURNAL_MAX_BYTES
                          if max_bytes is None else max_bytes)
        _positive_int(self.max_bytes, "max_bytes")
        self.scope = memory_scope(self.cwd)
        self.data_root = config.data_dir(self.cwd).resolve()
        scope_key = _digest(self.scope)
        session_key = _digest(self.session_id)
        self.directory = (self.data_root / "tui" / "events" /
                          scope_key / session_key)
        self.path = self.directory / "events.jsonl"
        self.meta_path = self.directory / "meta.json"
        self.summary_path = self.directory / "summary.json"
        self.checkpoint_path = self.directory / "checkpoint.json"
        self.lock_path = self.directory / "journal.lock"
        self.read_only_path = self.directory / "read-only.json"
        self.archive_path = self.directory / "archived.json"
        self.intent_path = self.directory / "append-intent.json"
        self.commit_path = self.directory / "append-commit.json"
        self._thread_lock = threading.RLock()
        self._layout_synced = False
        self._ambiguous_write = False
        self._known_size = -1
        self._event_ids: set[str] = set()
        self._last_seq_by_run: dict[str, int] = {}
        self._events: list[RuntimeEvent] = []
        self._warnings: tuple[str, ...] = ()
        self._truncated_tail = False
        self._degraded = False
        self._recovery: Optional[JournalRecovery] = None

    def recover(self) -> JournalRecovery:
        """Return the trusted prefix, repairing only a crash-truncated tail."""
        self._validate_current_boundary()
        with self._thread_lock, self._locked():
            self._ensure_meta()
            descriptor = self._open_journal(create=False)
            if descriptor is None:
                recovery = self._decorate_recovery(JournalRecovery())
                self._install_recovery(recovery)
                return recovery
            try:
                try:
                    intent_warnings = self._resolve_append_intent(descriptor)
                    recovery = self._scan_descriptor(
                        descriptor, initial_warnings=intent_warnings)
                except RuntimeEventJournalError as exc:
                    recovery = JournalRecovery(
                        warnings=(f"unresolved journal append intent: {exc}",),
                        degraded=True,
                        total_bytes=os.fstat(descriptor).st_size)
                recovery = self._decorate_recovery(recovery)
            finally:
                os.close(descriptor)
            self._install_recovery(recovery)
            return recovery

    def append(self, event: RuntimeEvent) -> bool:
        """Durably append one canonical event; identical ids are idempotent."""
        if not isinstance(event, RuntimeEvent):
            raise TypeError("event must be a RuntimeEvent")
        self._validate_current_boundary()
        if event.session_id != self.session_id:
            raise RuntimeEventJournalError("event session does not match journal")
        _reject_secret_envelope(event)
        persisted = _redacted_event(event)
        if persisted.seq <= 0:
            raise RuntimeEventJournalError("journal events require a positive seq")
        line = persisted.to_jsonl().encode("utf-8")
        with self._thread_lock, self._locked():
            self._ensure_meta()
            self._ensure_writable()
            descriptor = self._open_journal(create=True)
            assert descriptor is not None
            try:
                self._refresh_if_changed(descriptor)
                self._ensure_writable()
                if persisted.event_id in self._event_ids:
                    return False
                self._validate_next_sequence(persisted)
                current = os.fstat(descriptor).st_size
                if current + len(line) > self.max_bytes:
                    raise RuntimeEventJournalError(
                        "runtime event journal capacity exhausted",
                        journal_fatal=True)
                self._write_line(descriptor, current, line)
                self._remember(persisted, current + len(line))
            finally:
                os.close(descriptor)
        return True

    def mark_read_only(self, reason: str) -> None:
        """Persist a shared fail-closed marker after semantic corruption."""
        self._validate_current_boundary()
        detail = _bounded_text(redact(str(reason or "semantic corruption")), 2048)
        with self._thread_lock, self._locked():
            self._ensure_meta()
            self._write_read_only_marker(detail)
            self._degraded = True
            self._warnings = tuple(dict.fromkeys(
                (*self._warnings, f"journal marked read-only: {detail}")))
            self._recovery = None

    def mark_archived(self, *, expected_bytes: Optional[int] = None) -> bool:
        """Atomically make an existing journal locally read-only and recoverable."""
        return tui_journal_archive.mark_archived(self, expected_bytes)

    def restore_archived(self) -> bool:
        """Remove only a valid local archive marker; event history is untouched."""
        return tui_journal_archive.restore_archived(self)

    @property
    def archived(self) -> bool:
        """Return the validated local archive state without creating storage."""
        return tui_journal_archive.is_archived(self)

    @property
    def recovery(self) -> JournalRecovery:
        if self._recovery is None:
            size = max(0, self._known_size)
            self._recovery = JournalRecovery(
                events=tuple(self._events), warnings=self._warnings,
                truncated_tail=self._truncated_tail,
                degraded=self._degraded, trusted_bytes=size,
                total_bytes=size)
        return self._recovery

    @property
    def identity(self) -> tuple:
        return (
            str(self.data_root), self.cwd, self.session_id,
            tuple(sorted(self.scope.items())),
        )

    def _refresh_if_changed(self, descriptor: int) -> None:
        intent_warnings = self._resolve_append_intent(descriptor)
        current = os.fstat(descriptor).st_size
        if self._known_size < 0 or current != self._known_size or intent_warnings:
            recovery = self._decorate_recovery(
                self._scan_descriptor(
                    descriptor, initial_warnings=intent_warnings))
            self._install_recovery(recovery)

    def _scan_descriptor(
            self, descriptor: int, *,
            initial_warnings: tuple[str, ...] = ()) -> JournalRecovery:
        total = os.fstat(descriptor).st_size
        if total > self.max_bytes:
            return JournalRecovery(
                warnings=("journal exceeds configured file limit",),
                degraded=True, total_bytes=total)
        events: list[RuntimeEvent] = []
        warnings = list(initial_warnings)
        event_ids: set[str] = set()
        last_seq: dict[str, int] = {}
        trusted = 0
        truncated = degraded = False
        with os.fdopen(os.dup(descriptor), "rb") as handle:
            handle.seek(0)
            line_number = 0
            while True:
                record = _read_record(handle)
                if record is None:
                    break
                line_number += 1
                if not record.complete:
                    os.ftruncate(descriptor, record.start)
                    os.fsync(descriptor)
                    warnings.append(f"discarded crash-truncated journal tail at line {line_number}")
                    trusted, truncated = record.start, True
                    break
                error, event = self._decode_record(
                    record, event_ids, last_seq)
                if error:
                    warnings.append(f"stopped at untrusted journal line {line_number}: {error}")
                    degraded = True
                    break
                assert event is not None
                events.append(event)
                if (event.payload.get("journal_payload_truncated") is True
                        or event.extensions.get("journal_payload_truncated") is True):
                    warnings.append(
                        f"journal event payload compacted at line {line_number}")
                event_ids.add(event.event_id)
                last_seq[event.run_id] = event.seq
                trusted = record.end
        self._event_ids, self._last_seq_by_run = event_ids, last_seq
        actual = os.fstat(descriptor).st_size
        return JournalRecovery(
            events=tuple(events), warnings=tuple(warnings),
            truncated_tail=truncated, degraded=degraded,
            trusted_bytes=trusted, total_bytes=total if not truncated else actual)

    def _decode_record(self, record: _RawRecord, event_ids: set[str],
                       last_seq: dict[str, int]) -> tuple[str, Optional[RuntimeEvent]]:
        if record.oversized:
            return "record exceeds line limit", None
        try:
            line = record.raw.decode("utf-8", errors="strict")
            event = RuntimeEvent.from_jsonl(line)
        except (UnicodeDecodeError, ValueError) as exc:
            return str(exc), None
        if event.session_id != self.session_id:
            return "event session does not match journal", None
        if event.seq <= 0:
            return "journal event has non-positive seq", None
        if event.event_id in event_ids:
            return "duplicate event_id", None
        previous = last_seq.get(event.run_id)
        if previous is None and len(last_seq) >= MAX_JOURNAL_RUNS:
            return "journal run limit exceeded", None
        if previous is not None and event.seq <= previous:
            return "non-monotonic run sequence", None
        return "", event

    def _ensure_writable(self) -> None:
        tui_journal_archive.ensure_writable(self)
        marker = self._read_only_reason()
        if marker:
            self._degraded = True
        if self._degraded or self._ambiguous_write:
            raise RuntimeEventJournalError(
                "runtime event journal is degraded and read-only",
                journal_fatal=True)

    def _validate_next_sequence(self, event: RuntimeEvent) -> None:
        previous = self._last_seq_by_run.get(event.run_id)
        if previous is None and len(self._last_seq_by_run) >= MAX_JOURNAL_RUNS:
            raise RuntimeEventJournalError(
                "runtime event journal run limit exhausted",
                journal_fatal=True)
        if previous is not None and event.seq <= previous:
            raise RuntimeEventJournalError(
                f"non-monotonic journal seq for run {event.run_id!r}")

    def _write_line(self, descriptor: int, current: int, line: bytes) -> None:
        intent = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "state": "pending",
            "start": current,
            "length": len(line),
            "sha256": hashlib.sha256(line).hexdigest(),
        }
        _atomic_json(self.intent_path, intent)
        try:
            written = _write_once(descriptor, line)
            if written != len(line):
                raise OSError("short journal write")
            _fsync_file(descriptor)
        except OSError as exc:
            self._raise_append_failure(
                descriptor, current, "cannot append", exc, intent)
        committed = dict(intent)
        committed["state"] = "committed"
        try:
            _atomic_json(self.commit_path, committed)
        except RuntimeEventJournalError as exc:
            self._raise_append_failure(
                descriptor, current, "cannot commit", exc, intent)
        self._finalize_commit(descriptor, current, intent)

    def _raise_append_failure(self, descriptor: int, current: int,
                              action: str, cause: Exception,
                              intent: dict) -> None:
        rolled_back = _rollback_append(descriptor, current)
        if rolled_back:
            self._clear_rollback_markers()
        else:
            _atomic_json_best_effort(self.intent_path, intent)
            self._ambiguous_write = True
            self._degraded = True
            self._write_read_only_marker(
                f"{action} append has an untrusted outcome", best_effort=True)
        raise RuntimeEventJournalError(
            f"{action} runtime event journal append: {cause}",
            retryable=rolled_back,
            journal_fatal=not rolled_back) from cause

    def _clear_rollback_markers(self) -> None:
        if _clear_json_best_effort(self.commit_path):
            _clear_json_best_effort(self.intent_path)

    def _finalize_commit(self, descriptor: int, current: int,
                         intent: dict) -> None:
        try:
            _clear_json(self.intent_path)
        except RuntimeEventJournalError as exc:
            if not self.intent_path.exists():
                try:
                    _fsync_directory(self.directory)
                except RuntimeEventJournalError:
                    pass
                else:
                    _clear_json_best_effort(self.commit_path)
                    return
            self._raise_append_failure(
                descriptor, current, "cannot finalize", exc, intent)
        _clear_json_best_effort(self.commit_path)

    def _remember(self, event: RuntimeEvent, size: int) -> None:
        self._event_ids.add(event.event_id)
        self._last_seq_by_run[event.run_id] = event.seq
        self._events.append(event)
        self._known_size = size
        self._recovery = None

    def _install_recovery(self, recovery: JournalRecovery) -> None:
        recovery = self._decorate_recovery(recovery)
        self._recovery = recovery
        self._known_size = recovery.total_bytes
        self._events = list(recovery.events)
        self._warnings = recovery.warnings
        self._truncated_tail = recovery.truncated_tail
        self._degraded = recovery.degraded
        self._event_ids = {event.event_id for event in recovery.events}
        self._last_seq_by_run = {}
        for event in recovery.events:
            self._last_seq_by_run[event.run_id] = event.seq

    def _decorate_recovery(self, recovery: JournalRecovery) -> JournalRecovery:
        reasons = list(recovery.warnings)
        marker = self._read_only_reason()
        if marker:
            reasons.append(f"journal marked read-only: {marker}")
        if self._ambiguous_write:
            reasons.append("previous journal append has ambiguous durability")
        degraded = bool(recovery.degraded or marker or self._ambiguous_write)
        if degraded == recovery.degraded and tuple(reasons) == recovery.warnings:
            return recovery
        return JournalRecovery(
            events=recovery.events, warnings=tuple(dict.fromkeys(reasons)),
            truncated_tail=recovery.truncated_tail, degraded=degraded,
            trusted_bytes=recovery.trusted_bytes,
            total_bytes=recovery.total_bytes)

    def _read_only_reason(self) -> str:
        return str(self._read_only_marker().get("reason", ""))

    def _read_only_marker(self) -> dict:
        if not self.read_only_path.exists() and not self.read_only_path.is_symlink():
            return {}
        marker = _read_json(self.read_only_path)
        if marker.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            raise RuntimeEventJournalError("journal read-only marker schema mismatch")
        reason = marker.get("reason")
        if not isinstance(reason, str) or not reason:
            raise RuntimeEventJournalError("journal read-only marker is invalid")
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "reason": _bounded_text(reason, 2048),
        }

    def _write_read_only_marker(
            self, reason: str, *, best_effort: bool = False) -> None:
        if self.read_only_path.exists() or self.read_only_path.is_symlink():
            return
        marker = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "reason": _bounded_text(redact(str(reason)), 2048),
        }
        if best_effort:
            _atomic_json_best_effort(self.read_only_path, marker)
        else:
            _atomic_json(self.read_only_path, marker)

    @contextlib.contextmanager
    def _locked(self):
        self._ensure_layout()
        descriptor = _open_nofollow(
            self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _require_regular(descriptor, "journal lock")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _ensure_layout(self) -> None:
        base = self.data_root
        if base.is_symlink():
            raise RuntimeEventJournalError(f"refusing symlink data directory: {base}")
        base_created = not base.exists()
        base.mkdir(parents=True, exist_ok=True)
        if base_created or not self._layout_synced:
            _fsync_directory(base.parent)
        current = base
        for name in ("tui", "events", self.directory.parent.name,
                     self.directory.name):
            candidate = current / name
            if candidate.is_symlink():
                raise RuntimeEventJournalError(f"refusing symlink journal directory: {candidate}")
            created = not candidate.exists()
            candidate.mkdir(mode=0o700, exist_ok=True)
            if not candidate.is_dir():
                raise RuntimeEventJournalError(f"journal path is not a directory: {candidate}")
            os.chmod(candidate, 0o700)
            if created or not self._layout_synced:
                _fsync_directory(current)
            current = candidate
        self._layout_synced = True

    def _ensure_meta(self) -> None:
        expected = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "scope": self.scope,
            "session_id": self.session_id,
            "cwd_id": _digest(self.cwd),
        }
        if not self.meta_path.exists() and not self.meta_path.is_symlink():
            _atomic_json(self.meta_path, expected)
        else:
            actual = _read_json(self.meta_path)
            if actual != expected:
                raise RuntimeEventJournalError("journal metadata boundary mismatch")
            if not scope_matches(actual, self.scope):
                raise RuntimeEventJournalError("journal scope does not match current boundary")
        _fsync_directory(self.directory)

    def _validate_current_boundary(self) -> None:
        if memory_scope(self.cwd) != self.scope:
            raise RuntimeEventJournalError("journal scope changed after binding")
        if config.data_dir(self.cwd).resolve() != self.data_root:
            raise RuntimeEventJournalError("journal data directory changed after binding")

    def _open_journal(self, *, create: bool) -> Optional[int]:
        flags = os.O_RDWR | os.O_APPEND
        if create:
            flags |= os.O_CREAT
        try:
            descriptor = _open_nofollow(self.path, flags, 0o600)
        except FileNotFoundError:
            return None
        try:
            _require_regular(descriptor, "runtime event journal")
            os.fchmod(descriptor, 0o600)
            if create:
                _fsync_directory(self.directory)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _resolve_append_intent(self, descriptor: int) -> tuple[str, ...]:
        """Resolve the durable write-ahead marker before trusting the tail."""
        has_intent = self.intent_path.exists() or self.intent_path.is_symlink()
        has_commit = self.commit_path.exists() or self.commit_path.is_symlink()
        if not has_intent and not has_commit:
            return ()
        source = self.intent_path if has_intent else self.commit_path
        intent = _validated_intent(_read_json(source), self.max_bytes)
        start, length = intent["start"], intent["length"]
        current = os.fstat(descriptor).st_size
        if has_intent:
            if intent["state"] != "pending":
                raise RuntimeEventJournalError(
                    "journal append intent is not pending")
            if current < start:
                raise RuntimeEventJournalError(
                    "pending append intent begins beyond the journal")
            try:
                os.ftruncate(descriptor, start)
                os.fsync(descriptor)
            except OSError as exc:
                raise RuntimeEventJournalError(
                    f"cannot roll back pending journal append: {exc}",
                    journal_fatal=True) from exc
            self._clear_rollback_markers()
            return ("rolled back an uncommitted journal append",)
        if has_commit:
            if intent["state"] != "committed":
                raise RuntimeEventJournalError(
                    "journal append commit marker is not committed")
            end = start + length
            raw = os.pread(descriptor, length, start)
            if current != end or hashlib.sha256(raw).hexdigest() != intent["sha256"]:
                raise RuntimeEventJournalError(
                    "journal append commit does not match journal content")
            _clear_json_best_effort(self.commit_path)
            return ("confirmed a committed journal append",)
        return ()


def _read_record(handle) -> Optional[_RawRecord]:
    start = handle.tell()
    raw = handle.readline(MAX_EVENT_LINE_BYTES + 1)
    if not raw:
        return None
    if len(raw) <= MAX_EVENT_LINE_BYTES:
        return _RawRecord(start, handle.tell(), raw, raw.endswith(b"\n"), False)
    chunks = [raw] if raw.endswith(b"\n") else []
    complete = raw.endswith(b"\n")
    while not complete:
        part = handle.readline(MAX_EVENT_LINE_BYTES + 1)
        if not part:
            break
        complete = part.endswith(b"\n")
    return _RawRecord(start, handle.tell(), b"".join(chunks), complete, True)


def _redacted_event(event: RuntimeEvent) -> RuntimeEvent:
    mapping = json.loads(event.to_jsonl())
    _reject_secret_mapping_keys(mapping)
    safe = _redact_journal_value(mapping)
    attach_plan_projection_identities(mapping, safe, _identity_digest)
    for name in (
            "schema_version", "event_id", "seq", "occurred_at",
            "workspace_id", "session_id", "run_id", "turn_id",
            "actor_id", "kind"):
        safe[name] = mapping[name]
    preserve_actor_lineage(mapping, safe)
    try:
        return _event_from_mapping(safe)
    except ValueError:
        compact = _compact_redacted_event(mapping, safe)
        try:
            return _event_from_mapping(compact)
        except ValueError:
            relocated = _relocate_compaction_markers(compact)
            try:
                return _event_from_mapping(relocated)
            except ValueError:
                identities = _relocate_projection_identities(relocated)
                try:
                    return _event_from_mapping(identities)
                except ValueError as exc:
                    raise RuntimeEventJournalError(
                        "redacted runtime event cannot satisfy protocol bounds") from exc


def _redact_journal_value(value, field: str = ""):
    if field in RUNTIME_IDENTITY_FIELDS and value is not None:
        if not isinstance(value, (str, int, float, bool)):
            raise RuntimeEventJournalError(
                f"runtime event {field} identity must be a scalar")
        if field == "item_id" and not value:
            return ""
        return _identity_digest(value)
    if isinstance(value, str):
        cleaned = redact(value)
        if cleaned != value and len(cleaned.encode("utf-8")) > len(value.encode("utf-8")):
            return "[R]" if len(value.encode("utf-8")) >= 3 else ""
        return cleaned
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            result[key] = ("" if _secret_key(name)
                           else _redact_journal_value(item, name))
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_journal_value(item, field) for item in value]
    return redact_content(value)


def _secret_key(value: str) -> bool:
    marker = redact_content({value: "journal-secret-marker"})
    return marker.get(value) == "[REDACTED]"


def _identity_digest(value) -> str:
    """Use one collision-resistant namespace for every persisted identity."""
    normalized = str(value)
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return "id:" + encoded


def stable_plan_identity(value: object) -> str:
    """Return the journal's bounded pseudonym for a plan identity."""
    if not value:
        return ""
    return _identity_digest(value)


def _event_from_mapping(value: dict) -> RuntimeEvent:
    line = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return RuntimeEvent.from_jsonl(line)


def _compact_redacted_event(original: dict, safe: dict) -> dict:
    payload = safe.get("payload", {})
    digest = hashlib.sha256(_json_bytes(payload)).hexdigest()
    compact_payload = _projection_payload(str(original.get("kind", "")), payload)
    compact_payload.update({
        "journal_payload_truncated": True,
        "journal_payload_sha256": digest,
    })
    compact = {name: original[name] for name in (
        "schema_version", "event_id", "seq", "occurred_at", "workspace_id",
        "session_id", "run_id", "turn_id", "actor_id", "kind")}
    compact["payload"] = compact_payload
    compact["extensions"] = _compact_extensions(safe.get("extensions", {}))
    return compact


def _projection_payload(kind: str, payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    if kind.startswith("session."):
        return _picked(
            payload, "provider_session_id", "workspace_id", "session_id",
            "state", "mode")
    if kind.startswith("message."):
        return _picked(payload, "item_id", "text", "message")
    if kind.startswith("turn."):
        return _picked(payload, "state", "reason", "returncode")
    if kind == "workspace.snapshot":
        return _workspace_projection_payload(payload)
    if kind.startswith("runtime."):
        return _picked(payload, "stream", "text", "message", "reason", "raw")
    if kind in {"command.started", "command.completed"}:
        return _picked(payload, "item_id", "command", "exit_code", "status")
    if kind == "command.output":
        return _picked(payload, "item_id", "aggregated_output", "output")
    if kind == "file.changed":
        return _file_projection_payload(payload)
    if kind == "plan.updated":
        return plan_projection_payload(payload)
    if kind == "usage.updated":
        return _usage_projection_payload(payload)
    return {}


def _picked(payload: dict, *names: str) -> dict:
    return {name: payload[name] for name in names if name in payload}


def _workspace_projection_payload(payload: dict) -> dict:
    result = _picked(payload, "phase")
    workspace = payload.get("workspace", payload)
    if isinstance(workspace, dict):
        result["workspace"] = _picked(
            workspace, "available", "root", "branch", "head", "changed")
    delta = payload.get("delta", payload.get("last_turn_workspace"))
    if isinstance(delta, dict):
        result["delta"] = _picked(
            delta, "changed", "dirty_files", "paths",
            "head_changed", "paths_truncated")
    return result


def _file_projection_payload(payload: dict) -> dict:
    paths = list(payload.get("paths", [])) if isinstance(
        payload.get("paths"), (list, tuple)) else []
    if payload.get("path"):
        paths.append(payload["path"])
    changes = payload.get("changes", [])
    if isinstance(changes, list):
        paths.extend(item.get("path") if isinstance(item, dict) else item
                     for item in changes)
    return {"paths": [item for item in paths if item]}


def _usage_projection_payload(payload: dict) -> dict:
    nested = payload.get("usage")
    if isinstance(nested, dict):
        return {"usage": _numeric_values(nested)}
    return _numeric_values(payload)


def _numeric_values(value: dict) -> dict:
    return {key: item for key, item in value.items()
            if isinstance(item, (int, float)) and not isinstance(item, bool)}


def _compact_extensions(value) -> dict:
    if not isinstance(value, dict):
        return {"journal_payload_truncated": True}
    result = {"journal_payload_truncated": True}
    for key in ("runtime", "authoritative"):
        item = value.get(key)
        if isinstance(item, str):
            result[key] = _bounded_text(item, 1024)
        elif isinstance(item, bool):
            result[key] = item
    return result


def _relocate_compaction_markers(value: dict) -> dict:
    relocated = dict(value)
    payload = dict(value.get("payload", {}))
    extensions = dict(value.get("extensions", {}))
    for key in ("journal_payload_truncated", "journal_payload_sha256"):
        if key in payload:
            extensions[key] = payload.pop(key)
    relocated["payload"] = payload
    relocated["extensions"] = extensions
    return relocated


def _relocate_projection_identities(value: dict) -> dict:
    relocated = dict(value)
    payload = dict(value.get("payload", {}))
    kind = str(value.get("kind", ""))
    identities = {}
    if (kind.startswith("message.") or kind.startswith("command.")
            or kind == "plan.updated") and "item_id" in payload:
        identities["item_id"] = payload.pop("item_id")
    if not identities:
        return relocated
    extensions = dict(value.get("extensions", {}))
    extensions["journal_payload_identities"] = identities
    relocated["payload"] = payload
    relocated["extensions"] = extensions
    return relocated

def _read_json(path: Path) -> dict:
    if path.is_symlink():
        raise RuntimeEventJournalError(f"refusing symlink journal metadata: {path}")
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeEventJournalError("journal metadata must be a private regular file")
    descriptor = _open_nofollow(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(descriptor)
        _require_regular(descriptor, "journal metadata")
        if info.st_size > MAX_JOURNAL_META_BYTES:
            raise RuntimeEventJournalError("journal metadata exceeds size limit")
        raw = _read_bounded(descriptor, MAX_JOURNAL_META_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeEventJournalError("journal metadata is invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeEventJournalError("journal metadata must be an object")
    return value

def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise RuntimeEventJournalError(f"refusing symlink journal metadata: {path}")
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise RuntimeEventJournalError(
            f"cannot write journal metadata: {exc}", retryable=True) from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _validated_intent(value: dict, maximum: int) -> dict:
    expected = {"schema_version", "state", "start", "length", "sha256"}
    if set(value) != expected or value.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise RuntimeEventJournalError("journal append intent schema mismatch")
    if value.get("state") not in {"pending", "committed"}:
        raise RuntimeEventJournalError("journal append intent state is invalid")
    start, length = value.get("start"), value.get("length")
    if (isinstance(start, bool) or not isinstance(start, int) or start < 0
            or isinstance(length, bool) or not isinstance(length, int)
            or length <= 0 or length > MAX_EVENT_LINE_BYTES
            or start + length > maximum):
        raise RuntimeEventJournalError("journal append intent bounds are invalid")
    digest = value.get("sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)):
        raise RuntimeEventJournalError("journal append intent digest is invalid")
    return value


def _clear_json(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeEventJournalError(f"refusing symlink journal metadata: {path}")
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeEventJournalError(
            f"cannot clear journal metadata: {exc}", retryable=True) from exc
    _fsync_directory(path.parent)


def _clear_json_best_effort(path: Path) -> bool:
    try:
        _clear_json(path)
    except (OSError, RuntimeEventJournalError):
        return False
    return True


def _atomic_json_best_effort(path: Path, value: dict) -> bool:
    try:
        _atomic_json(path, value)
    except (OSError, RuntimeEventJournalError):
        return False
    return True


def _open_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags, mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeEventJournalError(f"refusing symlink journal path: {path}") from exc
        raise


def _require_regular(descriptor: int, label: str) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeEventJournalError(f"{label} must be a private regular file")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise RuntimeEventJournalError(
            f"cannot open journal directory for fsync: {path}: {exc}",
            retryable=True) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise RuntimeEventJournalError(
            f"cannot fsync journal directory: {path}: {exc}",
            retryable=True) from exc
    finally:
        os.close(descriptor)


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _write_once(descriptor: int, value: bytes) -> int:
    return os.write(descriptor, value)


def _rollback_append(descriptor: int, size: int) -> bool:
    try:
        os.ftruncate(descriptor, size)
        os.fsync(descriptor)
        return True
    except OSError:
        return False


def _bounded_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\r\n\0"):
        raise RuntimeEventJournalError(f"{label} must be a non-empty single-line string")
    if len(value.encode("utf-8")) > MAX_JOURNAL_ID_BYTES:
        raise RuntimeEventJournalError(f"{label} exceeds {MAX_JOURNAL_ID_BYTES} bytes")
    if redact(value) != value:
        raise RuntimeEventJournalError(f"{label} contains secret-like data")
    return value


def _reject_secret_envelope(event: RuntimeEvent) -> None:
    for name in (
            "event_id", "occurred_at", "workspace_id", "session_id",
            "run_id", "turn_id", "actor_id"):
        value = getattr(event, name)
        if isinstance(value, str) and value and redact(value) != value:
            raise RuntimeEventJournalError(
                f"runtime event {name} contains secret-like data")


def _reject_secret_mapping_keys(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if redact(str(key)) != str(key):
                raise RuntimeEventJournalError(
                    "runtime event mapping key contains secret-like data")
            _reject_secret_mapping_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_mapping_keys(item)


def _bounded_text(value: str, maximum: int) -> str:
    encoded = str(value or "").encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return encoded.decode("utf-8", "replace")
    return encoded[-maximum:].decode("utf-8", "ignore")


def _json_bytes(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]

def _positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
