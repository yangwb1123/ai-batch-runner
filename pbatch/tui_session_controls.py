"""Typed, non-blocking controls for the active runtime session."""

from __future__ import annotations

import json
import textwrap
from typing import Any, Optional

from .tui_runtime import (
    ActiveSessionRequest,
    CompactSessionRequest,
    CompactionHandle,
    RuntimeCompactionResult,
    RuntimeSessionState,
    RuntimeSessionStats,
)
from .tui_shell import SLASH_HELP
from .tui_view_support import clean_text, one_line


_RENDER_MAX_BYTES = 32 * 1024
_RENDER_MAX_LINES = 64
_SUMMARY_MAX_BYTES = 12 * 1024


def session_compaction_active(owner: Any) -> bool:
    """Return the optional logical compaction lock for mixin-only callers."""
    check = getattr(owner, "_session_compaction_running", None)
    return bool(check()) if callable(check) else False


def render_session_control(
        state: RuntimeSessionState, stats: RuntimeSessionStats,
        *, width: int = 80) -> str:
    """Render redacted session facts without exposing provider file paths."""
    width = _render_width(width)
    model = "unknown"
    if state.model is not None:
        model = f"{state.model.provider}/{state.model.model_id}"
    context = stats.context_usage
    lines = [
        "## Runtime session", "",
        _field("Session", state.session_id, width),
        _field("Name", state.name or "unknown", width),
        _field("Model", model, width),
        _field("Thinking", state.thinking_level or "unknown", width),
        _field("Streaming", _truth(state.is_streaming), width),
        _field("Compacting", _truth(state.is_compacting), width),
        _field("Auto compact", _truth(state.auto_compaction_enabled), width),
        _field("Messages", _number(state.message_count), width),
        _field("Pending", _number(state.pending_message_count), width),
        _field("Stats total", _number(stats.total_messages), width),
        _field("User / assistant", _pair(
            stats.user_messages, stats.assistant_messages), width),
        _field("Tool calls / results", _pair(
            stats.tool_calls, stats.tool_results), width),
        _field("Tokens", _number(stats.tokens.total_tokens), width),
        _field("Input / output", _pair(
            stats.tokens.input_tokens, stats.tokens.output_tokens), width),
        _field("Cache read / write", _pair(
            stats.tokens.cache_read_tokens,
            stats.tokens.cache_write_tokens), width),
        _field("Context", _context(context), width),
        _field("Cost", _cost(stats.cost), width),
    ]
    return _bounded_render(lines, width)


def render_compaction_result(
        result: RuntimeCompactionResult, *, width: int = 80) -> str:
    """Render a bounded compaction result and its retained summary."""
    width = _render_width(width)
    lines = [
        "## Session compacted", "",
        _field("Session", result.session_id, width),
        _field("Tokens before", _number(result.tokens_before), width),
        _field("Estimated after", _number(
            result.estimated_tokens_after), width),
        _field("Compaction tokens", _number(
            result.tokens.total_tokens), width),
        _field("Input / output", _pair(
            result.tokens.input_tokens,
            result.tokens.output_tokens), width),
        _field("Cache read / write", _pair(
            result.tokens.cache_read_tokens,
            result.tokens.cache_write_tokens), width),
        _field("Cost", _cost(result.cost), width),
        _field("Summary truncated", "yes" if result.summary_truncated
               else "no", width),
        "", "### Retained summary", "",
    ]
    lines.extend(_summary_lines(result.summary, width))
    return _bounded_render(lines, width)


class TUISessionControlMixin:
    """Expose provider-neutral state, stats, and lossy compaction commands."""

    def _init_session_controls(self) -> None:
        self._session_control_generation = 0
        self._session_control_pending = False
        self._session_compaction_generation = 0
        self._session_compaction_handle: Optional[CompactionHandle] = None
        self._session_compaction_adapter: Any = None
        self._session_compaction_session_id = ""
        initializer = getattr(self, "_init_model_controls", None)
        if callable(initializer):
            initializer()

    def _slash_help(self, args: tuple[str, ...]) -> None:
        if self._require_no_args("help", args):
            self._append_entry("system", SLASH_HELP)

    def _slash_status(self, args: tuple[str, ...]) -> None:
        if not self._require_no_args("status", args):
            return
        payload = self._backend_snapshot()
        self._append_entry("system", "```json\n" + json.dumps(
            payload, ensure_ascii=False, indent=2, default=str) + "\n```")
        self._refresh_status()

    def _slash_session(self, args: tuple[str, ...]) -> None:
        if not self._require_no_args("session", args):
            return
        if getattr(self, "_runtime_setting_change_running", lambda: False)():
            self.notify("Wait for the runtime setting operation to finish.",
                        severity="warning")
            return
        adapter, session_id = self._session_control_target(
            ("session_state", "session_stats"))
        if adapter is None:
            return
        generation = self._session_control_generation + 1
        self._session_control_generation = generation
        self._session_control_pending = True
        self._last_status = "Loading runtime session…"
        self._refresh_status()
        self.run_worker(
            lambda: self._load_session_control(
                generation, adapter, session_id),
            name="session-control", group="session-control",
            exit_on_error=False, exclusive=True, thread=True)

    def _load_session_control(
            self, generation: int, adapter: Any, session_id: str) -> None:
        state = stats = error = None
        try:
            request = ActiveSessionRequest(session_id)
            state = adapter.get_session_state(request)
            stats = adapter.get_session_stats(request)
        except Exception as exc:  # provider and transport errors are display data
            error = exc
        if getattr(self, "is_mounted", True) is False:
            return
        try:
            self.call_from_thread(
                self._finish_session_control, generation, adapter,
                session_id, state, stats, error)
        except RuntimeError:
            pass

    def _finish_session_control(
            self, generation: int, adapter: Any, session_id: str,
            state: Any, stats: Any, error: Optional[Exception]) -> None:
        if generation != self._session_control_generation:
            return
        self._session_control_pending = False
        if not self._session_control_matches(
                generation, adapter, session_id):
            self._refresh_status()
            return
        if error is not None:
            self._append_entry(
                "error", f"Session query failed: {_error_text(error)}")
            self._last_status = "Session query failed"
        elif not _valid_session_result(state, stats, session_id):
            self._append_entry("error", "Session query returned invalid data.")
            self._last_status = "Session query failed"
        else:
            recorder = getattr(self, "_record_runtime_control_state", None)
            if callable(recorder):
                recorder(state)
            self._append_entry("system", render_session_control(
                state, stats, width=self._session_control_width()))
            self._last_status = "Runtime session refreshed"
        self._refresh_status()

    def _slash_compact(self, args: tuple[str, ...]) -> None:
        if getattr(self, "_runtime_setting_change_running", lambda: False)():
            self.notify("Wait for the runtime setting operation to finish.",
                        severity="warning")
            return
        if self._busy:
            self.notify("Wait for the active operation before compacting.",
                        severity="warning")
            return
        if self._session_compaction_running():
            self.notify("Session compaction is already running.",
                        severity="warning")
            return
        adapter, session_id = self._session_control_target(
            ("session_compact",))
        if adapter is None:
            return
        try:
            handle = adapter.start_compaction(CompactSessionRequest(
                session_id, " ".join(args).strip()))
        except Exception as exc:
            self._append_entry(
                "error", f"Session compaction failed: {_error_text(exc)}")
            return
        if not isinstance(handle, CompactionHandle):
            self._append_entry(
                "error", "Session compaction returned an invalid handle.")
            return
        self._start_session_compaction(handle, adapter, session_id)

    def _start_session_compaction(
            self, handle: CompactionHandle, adapter: Any,
            session_id: str) -> None:
        invalidate = getattr(self, "_invalidate_runtime_completions", None)
        if callable(invalidate):
            invalidate()
        generation = self._session_compaction_generation + 1
        self._session_compaction_generation = generation
        self._session_compaction_handle = handle
        self._session_compaction_adapter = adapter
        self._session_compaction_session_id = session_id
        self._last_status = "Compacting runtime session…"
        self._append_entry(
            "system", "Session compaction started in the background.")
        self._refresh_status()
        self.run_worker(
            lambda: self._wait_for_compaction(
                generation, adapter, session_id, handle),
            name="session-compaction", group="session-compaction",
            exit_on_error=False, exclusive=True, thread=True)

    def _wait_for_compaction(
            self, generation: int, adapter: Any, session_id: str,
            handle: CompactionHandle) -> None:
        result = error = None
        try:
            result = handle.result()
        except Exception as exc:  # indefinite wait ends on result or transport loss
            error = exc
        if getattr(self, "is_mounted", True) is False:
            return
        try:
            self.call_from_thread(
                self._finish_compaction, generation, adapter, session_id,
                handle, result, error)
        except RuntimeError:
            pass

    def _finish_compaction(
            self, generation: int, adapter: Any, session_id: str,
            handle: CompactionHandle, result: Any,
            error: Optional[Exception]) -> None:
        if generation != self._session_compaction_generation:
            return
        if not self._session_compaction_matches(
                generation, adapter, session_id, handle):
            if handle is self._session_compaction_handle:
                self._clear_session_compaction()
                self._refresh_status()
            return
        self._clear_session_compaction()
        if error is not None:
            self._append_entry(
                "error", f"Session compaction failed: {_error_text(error)}")
            self._last_status = "Session compaction failed"
        elif (not isinstance(result, RuntimeCompactionResult)
              or result.session_id != session_id):
            self._append_entry(
                "error", "Session compaction returned invalid data.")
            self._last_status = "Session compaction failed"
        else:
            self._append_entry("system", render_compaction_result(
                result, width=self._session_control_width()))
            self._last_status = "Runtime session compacted"
        self._refresh_status()

    def _session_control_target(
            self, capabilities: tuple[str, ...]) -> tuple[Any, str]:
        adapter = getattr(self, "_adapter", None)
        session_id = self._current_session_id()
        if adapter is None or not session_id:
            self._append_entry(
                "error", "No active typed runtime session is available.")
            return None, ""
        try:
            supported = all(
                self._runtime_capability(name)[1] for name in capabilities)
        except (OSError, RuntimeError, TypeError, ValueError):
            supported = False
        if not supported:
            names = ", ".join(capabilities)
            self._append_entry(
                "error", f"The active runtime does not support {names}.")
            return None, ""
        return adapter, session_id

    def _session_control_matches(
            self, generation: int, adapter: Any, session_id: str) -> bool:
        if generation != self._session_control_generation:
            return False
        if adapter is not getattr(self, "_adapter", None):
            return False
        if session_id != self._current_session_id():
            return False
        try:
            return all(self._runtime_capability(name)[1] for name in (
                "session_state", "session_stats"))
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def _session_compaction_matches(
            self, generation: int, adapter: Any, session_id: str,
            handle: CompactionHandle) -> bool:
        return bool(
            generation == self._session_compaction_generation
            and adapter is getattr(self, "_adapter", None)
            and adapter is self._session_compaction_adapter
            and session_id == self._current_session_id()
            and session_id == self._session_compaction_session_id
            and handle is self._session_compaction_handle)

    def _session_compaction_running(self) -> bool:
        handle = getattr(self, "_session_compaction_handle", None)
        return bool(
            handle is not None
            and self._session_compaction_adapter is getattr(
                self, "_adapter", None)
            and self._session_compaction_session_id
            == self._current_session_id())

    def _session_control_status_lines(self) -> list[str]:
        lines = []
        if self._session_control_pending:
            lines.append("session   loading")
        if self._session_compaction_running():
            lines.append("compact   running")
        model_lines = getattr(self, "_model_control_status_lines", None)
        if callable(model_lines):
            lines.extend(model_lines())
        return lines

    def _invalidate_session_controls(self) -> None:
        self._session_control_generation += 1
        self._session_control_pending = False
        self._session_compaction_generation += 1
        self._clear_session_compaction()
        invalidate = getattr(self, "_invalidate_model_controls", None)
        if callable(invalidate):
            invalidate()

    def _clear_session_compaction(self) -> None:
        self._session_compaction_handle = None
        self._session_compaction_adapter = None
        self._session_compaction_session_id = ""

    def _session_control_width(self) -> int:
        width = int(getattr(getattr(self, "size", None), "width", 80))
        return max(40, min(120, width - 4))


def _render_width(width: int) -> int:
    if isinstance(width, bool) or not isinstance(width, int):
        return 80
    return max(24, min(160, width))


def _valid_session_result(state: Any, stats: Any, session_id: str) -> bool:
    return bool(
        isinstance(state, RuntimeSessionState)
        and isinstance(stats, RuntimeSessionStats)
        and state.session_id == session_id
        and stats.session_id == session_id)


def _safe_text(value: Any) -> str:
    text = clean_text(value).replace("\t", " ")
    return "".join(char for char in text if char == "\n" or ord(char) >= 32)


def _field(label: str, value: Any, width: int) -> str:
    prefix = f"- {label}: "
    available = max(1, width - len(prefix))
    text = one_line(_safe_text(value)) or "unknown"
    if len(text) > available:
        text = text[:max(0, available - 1)] + "…"
    return prefix + text


def _number(value: Optional[int]) -> str:
    return "unknown" if value is None else str(value)


def _pair(left: Optional[int], right: Optional[int]) -> str:
    return f"{_number(left)} / {_number(right)}"


def _truth(value: Optional[bool]) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _context(value: Any) -> str:
    if value is None:
        return "unknown"
    usage = _pair(value.tokens, value.context_window)
    percent = "unknown" if value.percent is None else f"{value.percent:g}%"
    return f"{usage} ({percent})"


def _cost(value: Optional[float]) -> str:
    return "unknown" if value is None else f"${value:.6f}"


def _summary_lines(value: Any, width: int) -> list[str]:
    text = _safe_text(value) or "No retained summary was returned."
    encoded = text.encode("utf-8")
    if len(encoded) > _SUMMARY_MAX_BYTES:
        text = encoded[:_SUMMARY_MAX_BYTES - 16].decode(
            "utf-8", "ignore") + "\n…[truncated]"
    lines: list[str] = []
    for source in text.splitlines() or [""]:
        chunks = textwrap.wrap(
            source, width=max(1, width - 4), replace_whitespace=True,
            drop_whitespace=True, break_long_words=True,
            break_on_hyphens=False) or [""]
        lines.extend(f"    {chunk}" for chunk in chunks)
        if len(lines) >= _RENDER_MAX_LINES - 14:
            lines = lines[:_RENDER_MAX_LINES - 15] + ["    …[truncated]"]
            break
    return lines


def _bounded_render(lines: list[str], width: int) -> str:
    bounded = [_safe_text(line)[:width] for line in lines[:_RENDER_MAX_LINES]]
    text = "\n".join(bounded)
    encoded = text.encode("utf-8")
    if len(encoded) <= _RENDER_MAX_BYTES:
        return text
    return encoded[:_RENDER_MAX_BYTES - 16].decode(
        "utf-8", "ignore") + "\n…[truncated]"


def _error_text(error: BaseException) -> str:
    text = one_line(_safe_text(error)) or type(error).__name__
    encoded = text.encode("utf-8")
    if len(encoded) <= 2048:
        return text
    return encoded[:2045].decode("utf-8", "ignore") + "…"
