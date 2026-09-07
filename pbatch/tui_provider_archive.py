"""Exact provider-owned session archive operations for App Server."""

from __future__ import annotations

from typing import Any, Mapping

from .tui_codex_support import _request_value
from .tui_runtime import UnsupportedOperation

_MAX_PROVIDER_ID_BYTES = 2_048


def apply(adapter: Any, request: Any, *, archived: bool) -> None:
    """Archive or restore one provider thread without changing local history."""
    adapter._require_initialized()
    if not adapter.runtime_info().capabilities.supports("session_archive"):
        raise UnsupportedOperation("thread/archive" if archived else "thread/unarchive",
                                   "session_archive")
    provider_id = _provider_id(request)
    _validate_workspace(adapter, request)
    operation = "provider-archive" if archived else "provider-unarchive"
    with adapter._lock:
        if adapter._active_operation or (
                adapter._active is not None and not adapter._active.done):
            raise RuntimeError("cannot change provider archive state during an active operation")
        if archived and provider_id == adapter.session.provider_session_id:
            raise RuntimeError("the active provider session cannot be archived")
        adapter._active_operation = operation
    try:
        result = adapter._call(
            "thread/archive" if archived else "thread/unarchive",
            {"threadId": provider_id})
        if not archived:
            _confirm_unarchive(result, provider_id)
    finally:
        with adapter._lock:
            if adapter._active_operation == operation:
                adapter._active_operation = ""


def _provider_id(request: Any) -> str:
    value = _request_value(request, "provider_session_id", "")
    if not isinstance(value, str) or not value:
        raise ValueError("provider_session_id is required for provider archive")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError("provider_session_id must be valid UTF-8") from exc
    if size > _MAX_PROVIDER_ID_BYTES:
        raise ValueError("provider_session_id is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("provider_session_id contains control characters")
    return value


def _validate_workspace(adapter: Any, request: Any) -> None:
    supplied = _request_value(request, "workspace_id", "")
    if supplied not in (None, "") and supplied != adapter.session.workspace_id:
        raise ValueError("provider archive workspace does not match the active runtime")


def _confirm_unarchive(result: Any, provider_id: str) -> None:
    thread = result.get("thread") if isinstance(result, Mapping) else None
    if not isinstance(thread, Mapping) or thread.get("id") != provider_id:
        raise RuntimeError("thread/unarchive response did not confirm the provider thread")
