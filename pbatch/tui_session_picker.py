"""Keyboard-first session picker kept outside the conversation transcript."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option
from rich.cells import cell_len, set_cell_size

from .tui_view_support import clean_text, one_line


@dataclass(frozen=True)
class SessionPickerAction:
    """One explicit action returned to the owning TUI application."""

    operation: str
    session_id: str = ""


class TUISessionPickerScreen(ModalScreen[Optional[SessionPickerAction]]):
    """A bounded picker for trusted records in the current workspace scope."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("q", "close", "Close", show=False),
        Binding("f", "fork", "Fork"),
        Binding("v", "native_recover", "Native view"),
        Binding("a", "archive", "Archive"),
        Binding("n", "new", "New"),
        Binding("r", "refresh", "Refresh"),
    ]
    CSS = """
    TUISessionPickerScreen {
        align: center middle;
        background: $background;
    }
    #session-picker-dialog {
        width: 88%;
        height: 82%;
        min-width: 38;
        background: $surface;
        border: round $accent;
    }
    #session-picker-heading {
        height: 3;
        padding: 1 2;
        text-style: bold;
        background: $boost;
    }
    #session-picker-options {
        height: 1fr;
        padding: 0 1;
    }
    #session-picker-detail {
        height: 7;
        padding: 0 2;
        color: $text-muted;
        border-top: solid $panel-lighten-1;
    }
    #session-picker-hint {
        height: 2;
        padding: 0 2;
        color: $text-muted;
    }
    """

    def __init__(self, index: Any, current_session: str = "") -> None:
        super().__init__(id="session-picker")
        self.index = index
        self.current_session = str(current_session or "")
        self._records = tuple(getattr(index, "sessions", ()))

    def compose(self) -> ComposeResult:
        options = [Option(
            _record_label(record, self.current_session),
            id=f"session-option-{position}",
        ) for position, record in enumerate(self._records)]
        with Vertical(id="session-picker-dialog"):
            yield Static(self._heading(), id="session-picker-heading", markup=False)
            yield OptionList(*options, id="session-picker-options", markup=False)
            yield Static(self._detail(0), id="session-picker-detail", markup=False)
            yield Static(
                "Enter resume/restore · v native view · f fork · "
                "a archive/restore · n new · r refresh · Esc close",
                id="session-picker-hint", markup=False)

    def on_mount(self) -> None:
        picker = self.query_one("#session-picker-options", OptionList)
        picker.focus()
        self._update_detail(picker.highlighted)

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        self._update_detail(event.option_index)

    def on_option_list_option_selected(
            self, event: OptionList.OptionSelected) -> None:
        record = self._record_at(event.option_index)
        if record is not None:
            operation = "restore" if getattr(record, "archived", False) else "resume"
            self.dismiss(SessionPickerAction(operation, record.session_id))

    def action_fork(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        if getattr(record, "archived", False):
            self.notify("Restore the archived session before forking it.", severity="warning")
            return
        if getattr(record, "read_only", False):
            self.notify("A read-only session cannot be forked.", severity="warning")
            return
        self.dismiss(SessionPickerAction("fork", record.session_id))

    def action_native_recover(self) -> None:
        record = self._selected_record()
        if record is None or getattr(record, "archived", False):
            return
        self.dismiss(SessionPickerAction("native-recover", record.session_id))

    def action_archive(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        operation = "restore" if getattr(record, "archived", False) else "archive"
        self.dismiss(SessionPickerAction(operation, record.session_id))

    def action_new(self) -> None:
        self.dismiss(SessionPickerAction("new"))

    def action_refresh(self) -> None:
        self.dismiss(SessionPickerAction("refresh"))

    def action_close(self) -> None:
        self.dismiss(None)

    def _heading(self) -> str:
        suffix: list[str] = []
        if getattr(self.index, "truncated", False):
            suffix.append("bounded list truncated")
        warnings = len(getattr(self.index, "warnings", ()))
        if warnings:
            suffix.append(f"{warnings} local index warning(s)")
        provider_warnings = len(getattr(self.index, "provider_warnings", ()))
        if provider_warnings:
            suffix.append(f"{provider_warnings} provider warning(s)")
        if getattr(self.index, "provider_truncated", False):
            suffix.append("provider page has more")
        detail = f" — {'; '.join(suffix)}" if suffix else ""
        return f"Sessions ({len(self._records)}){detail}"

    def _selected_record(self) -> Any:
        picker = self.query_one("#session-picker-options", OptionList)
        record = self._record_at(picker.highlighted)
        if record is None:
            self.notify("No session is selected.", severity="warning")
        return record

    def _record_at(self, index: Optional[int]) -> Any:
        if index is None or index < 0 or index >= len(self._records):
            return None
        return self._records[index]

    def _update_detail(self, index: Optional[int]) -> None:
        detail = self.query_one("#session-picker-detail", Static)
        width = max(24, int(detail.content_size.width or 72))
        detail.update(self._detail(index, width=width))

    def _detail(self, index: Optional[int], *, width: int = 160) -> str:
        record = self._record_at(index)
        if record is None:
            return _display(
                "No recoverable TUI sessions exist in this workspace scope.",
                width)
        states = _states(record, self.current_session)
        session_id = _display(record.session_id, 160)
        summary = getattr(record, "summary", None)
        local = _local_summary(summary, getattr(record, "summary_status", "missing"))
        provider = _provider_summary(
            getattr(record, "provider", None),
            getattr(self.index, "provider_observed_at", ""))
        preview = _summary_preview(record)
        lines = (
            session_id,
            f"Local · {_format_time(record.updated_ns)} · "
            f"{_format_bytes(record.journal_bytes)} · {', '.join(states)}",
            local,
            provider,
            f"Assistant preview · {preview}",
        )
        return "\n".join(_display(line, width) for line in lines)


def _record_label(record: Any, current_session: str) -> str:
    session_id = _display(record.session_id, 42)
    title = _record_title(record)
    states = ", ".join(_states(record, current_session))
    return (f"{title}  ·  {session_id}  ·  "
            f"{_format_time(record.updated_ns)}  ·  {states}")


def _states(record: Any, current_session: str) -> list[str]:
    states = ["current"] if record.session_id == current_session else []
    if getattr(record, "archived", False):
        states.append("archived")
    elif getattr(record, "read_only", False):
        states.append("read-only")
    else:
        states.append("candidate")
    return states


def _record_title(record: Any) -> str:
    provider = getattr(record, "provider", None)
    summary = getattr(record, "summary", None)
    value = (getattr(provider, "name", "") or
             getattr(provider, "preview", "") or
             getattr(summary, "preview", "") or "Untitled session")
    return _display(value, 56)


def _local_summary(summary: Any, status: str) -> str:
    if summary is None:
        return f"Local summary · unavailable ({_display(status, 24)})"
    values = [
        f"runtime {_display(getattr(summary, 'runtime', ''), 32)}",
        f"turn {_display(getattr(summary, 'turn_state', '') or 'none', 24)}",
    ]
    branch = getattr(summary, "branch", "")
    if branch:
        values.append(f"branch {_display(branch, 40)}")
    model = getattr(summary, "model", "")
    if model:
        values.append(f"model {_display(model, 40)}")
    return "Local summary · " + " · ".join(values)


def _provider_summary(provider: Any, observed_at: str) -> str:
    if provider is None:
        return "Provider advisory · unavailable on this bounded page"
    status = _display(getattr(provider, "status", "") or "unknown", 24)
    flags = tuple(getattr(provider, "active_flags", ()) or ())
    if flags:
        visible = tuple(_display(flag, 24) for flag in flags[:3])
        suffix = f", +{len(flags) - len(visible)} more" if len(flags) > 3 else ""
        status += f" ({', '.join(visible)}{suffix})"
    details = [status]
    model_provider = getattr(provider, "model_provider", "")
    if model_provider:
        details.append(f"model provider {_display(model_provider, 32)}")
    updated = getattr(provider, "updated_at", None)
    if updated is not None:
        details.append(f"updated {_format_epoch(updated)}")
    if observed_at:
        details.append(f"observed {_display(observed_at, 32)}")
    return "Provider advisory · " + " · ".join(details)


def _summary_preview(record: Any) -> str:
    provider = getattr(record, "provider", None)
    summary = getattr(record, "summary", None)
    value = (getattr(provider, "preview", "") or
             getattr(summary, "preview", "") or "unavailable")
    return _display(value, 180)


def _display(value: Any, limit: int) -> str:
    text = one_line(clean_text(value)) or "-"
    if cell_len(text) <= limit:
        return text
    prefix = set_cell_size(text, max(1, limit - 1)).rstrip()
    return prefix + "…"


def _format_time(updated_ns: int) -> str:
    try:
        value = datetime.fromtimestamp(updated_ns / 1_000_000_000, timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return "unknown time"
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def _format_epoch(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "unknown time"
    try:
        timestamp = datetime.fromtimestamp(value, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return "unknown time"
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")
