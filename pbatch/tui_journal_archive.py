"""Reversible local archive lifecycle for one trusted TUI journal."""

from __future__ import annotations

import os
import time
from typing import Any, Optional


ARCHIVE_SCHEMA_VERSION = 1


def mark_archived(journal: Any, expected_bytes: Optional[int]) -> bool:
    """Compare-and-mark one journal while holding its append lock."""
    _validate_expected_bytes(expected_bytes)
    journal._validate_current_boundary()
    with journal._thread_lock, journal._locked():
        journal._ensure_meta()
        if _read_marker(journal):
            return False
        _verify_size(journal, expected_bytes)
        _journal_module()._atomic_json(journal.archive_path, {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "session_id": journal.session_id,
            "archived_ns": time.time_ns(),
        })
    return True


def restore_archived(journal: Any) -> bool:
    """Remove only a validated marker while preserving event history."""
    journal._validate_current_boundary()
    with journal._thread_lock, journal._locked():
        journal._ensure_meta()
        if not _read_marker(journal):
            return False
        _journal_module()._clear_json(journal.archive_path)
    return True


def is_archived(journal: Any) -> bool:
    """Inspect archive state without creating a missing journal layout."""
    if not journal.directory.exists() and not journal.directory.is_symlink():
        return False
    journal._validate_current_boundary()
    return bool(_read_marker(journal))


def ensure_writable(journal: Any) -> None:
    """Reject event mutation while a validated archive marker exists."""
    if _read_marker(journal):
        raise _error("runtime event journal is archived")


def _read_marker(journal: Any) -> dict:
    path = journal.archive_path
    if not path.exists() and not path.is_symlink():
        return {}
    marker = _journal_module()._read_json(path)
    expected = {"schema_version", "session_id", "archived_ns"}
    archived_ns = marker.get("archived_ns")
    if (set(marker) != expected
            or marker.get("schema_version") != ARCHIVE_SCHEMA_VERSION
            or marker.get("session_id") != journal.session_id
            or isinstance(archived_ns, bool)
            or not isinstance(archived_ns, int) or archived_ns <= 0):
        raise _error("journal archive marker is invalid")
    return marker


def _verify_size(journal: Any, expected_bytes: Optional[int]) -> None:
    if expected_bytes is None:
        return
    descriptor = journal._open_journal(create=False)
    try:
        actual = os.fstat(descriptor).st_size if descriptor is not None else 0
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if actual != expected_bytes:
        raise _error(
            "runtime event journal changed while preparing archive",
            retryable=True)


def _validate_expected_bytes(value: Optional[int]) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_bytes must be a non-negative integer")


def _journal_module():
    from . import tui_journal
    return tui_journal


def _error(message: str, *, retryable: bool = False):
    return _journal_module().RuntimeEventJournalError(
        message, retryable=retryable)
