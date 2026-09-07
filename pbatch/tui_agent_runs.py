"""Bounded Agents Inspector backed by the shared terminal-run ledger."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .agent_ledger import LedgerQuerySnapshot, query_snapshot
from .tui_actor_tree import (
    ActorHistorySnapshot, ActorTreeSnapshot, render_actor_history,
    render_actor_tree,
)
from .tui_view_support import clean_text, one_line


AGENT_RUN_DISPLAY_LIMIT = 100
_DETAIL_MAX_BYTES = 8 * 1024


@dataclass(frozen=True)
class AgentRunRecord:
    source: str
    timestamp: str
    agent: str
    provider: str
    status: str
    task_brief: str
    attempt: int = 0
    elapsed_ms: int = 0
    exit_code: int = 0
    run_id: str = ""
    session_id: str = ""
    session_name: str = ""
    outputs: tuple[str, ...] = ()
    current: bool = False


@dataclass(frozen=True)
class AgentRunsSnapshot:
    records: tuple[AgentRunRecord, ...]
    skipped_records: int = 0
    truncated: bool = False
    error: str = ""
    ledger_bytes: int = 0
    actor_tree: ActorTreeSnapshot = field(default_factory=ActorTreeSnapshot)
    actor_history: ActorHistorySnapshot = field(
        default_factory=ActorHistorySnapshot)


def build_agent_runs_snapshot(
        ledger: LedgerQuerySnapshot,
        current: Optional[AgentRunRecord] = None,
        actor_tree: Optional[ActorTreeSnapshot] = None,
        actor_history: Optional[ActorHistorySnapshot] = None) -> AgentRunsSnapshot:
    """Normalize untrusted JSON rows and prepend the live coordinator fact."""
    records = [_ledger_record(value) for value in reversed(ledger.records)]
    normalized = [value for value in records if value is not None]
    skipped = ledger.skipped_records + len(records) - len(normalized)
    if current is not None:
        normalized.insert(0, current)
    truncated = ledger.truncated or len(normalized) > AGENT_RUN_DISPLAY_LIMIT
    return AgentRunsSnapshot(
        tuple(normalized[:AGENT_RUN_DISPLAY_LIMIT]), skipped, truncated,
        _text(ledger.error, 500), max(0, int(ledger.bytes_read or 0)),
        actor_tree if isinstance(actor_tree, ActorTreeSnapshot)
        else ActorTreeSnapshot(),
        actor_history if isinstance(actor_history, ActorHistorySnapshot)
        else ActorHistorySnapshot())


class TUIAgentRunsScreen(ModalScreen[None]):
    """Inspect active/recent agent runs without copying them into chat."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("q", "close", "Close", show=False),
        Binding("r", "refresh_runs", "Refresh"),
        Binding("a", "cycle_agent", "Agent filter"),
        Binding("s", "cycle_status", "Status filter"),
        Binding("h", "cycle_actor_history", "Actor history"),
    ]
    CSS = """
    TUIAgentRunsScreen { align: center middle; background: $background; }
    #agent-runs-dialog {
        width: 90%; height: 86%; min-width: 40;
        background: $surface; border: round $accent;
    }
    #agent-runs-heading {
        height: 3; padding: 1 2; text-style: bold; background: $boost;
    }
    #agent-runs-tree {
        height: auto; max-height: 9; padding: 0 2;
        color: $text-muted; border-bottom: solid $panel-lighten-1;
    }
    #agent-runs-options { height: 1fr; padding: 0 1; }
    #agent-runs-detail {
        height: 11; padding: 0 2; color: $text-muted;
        border-top: solid $panel-lighten-1;
    }
    #agent-runs-hint { height: 2; padding: 0 2; color: $text-muted; }
    """

    def __init__(self, snapshot: AgentRunsSnapshot,
                 loader: Callable[[], AgentRunsSnapshot],
                 *, agent_filter: str = "") -> None:
        super().__init__(id="agents-inspector")
        self.inspector_kind = "agents"
        self.snapshot = snapshot
        self._loader = loader
        self._agent_filter = _text(agent_filter, 64)
        self._status_filter = ""
        self._actor_history_index = -1
        self._visible: tuple[AgentRunRecord, ...] = ()
        self._apply_filter()

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-runs-dialog"):
            yield Static(self._heading(), id="agent-runs-heading", markup=False)
            yield Static(self._actor_tree(), id="agent-runs-tree", markup=False)
            yield OptionList(*self._options(), id="agent-runs-options", markup=False)
            yield Static(self._detail(0), id="agent-runs-detail", markup=False)
            yield Static(
                "↑/↓ inspect · a agent filter · s status filter · "
                "h actor history · r refresh · Esc close",
                id="agent-runs-hint", markup=False)

    def on_mount(self) -> None:
        options = self.query_one("#agent-runs-options", OptionList)
        options.focus()
        self._update_detail(options.highlighted)

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        self._update_detail(event.option_index)

    def action_refresh_runs(self) -> None:
        try:
            snapshot = self._loader()
        except Exception as exc:
            self.notify(f"Unable to refresh agent runs: {exc}", severity="error")
            return
        if not isinstance(snapshot, AgentRunsSnapshot):
            self.notify("Agent run loader returned an invalid snapshot.",
                        severity="error")
            return
        self.refresh_snapshot(snapshot)

    def action_cycle_agent(self) -> None:
        values = [""] + sorted({row.agent for row in self.snapshot.records})
        self._agent_filter = _next_filter(values, self._agent_filter)
        self._refresh_filter()

    def action_cycle_status(self) -> None:
        values = [""] + sorted({row.status for row in self.snapshot.records})
        self._status_filter = _next_filter(values, self._status_filter)
        self._refresh_filter()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_cycle_actor_history(self) -> None:
        groups = self.snapshot.actor_history.groups
        if not groups:
            self.notify("No trusted historical actor trees are available.",
                        severity="warning")
            return
        self._actor_history_index += 1
        if self._actor_history_index >= len(groups):
            self._actor_history_index = -1
        self._refresh_actor_tree()

    def refresh_snapshot(self, snapshot: AgentRunsSnapshot) -> None:
        self.snapshot = snapshot
        if self._actor_history_index > len(snapshot.actor_history.groups) - 1:
            self._actor_history_index = -1
        self._refresh_filter()

    def _refresh_filter(self) -> None:
        self._apply_filter()
        if not self.is_mounted:
            return
        options = self.query_one("#agent-runs-options", OptionList)
        options.set_options(self._options())
        options.highlighted = 0 if self._visible else None
        self.query_one("#agent-runs-heading", Static).update(self._heading())
        self._refresh_actor_tree()
        self._update_detail(options.highlighted)

    def _apply_filter(self) -> None:
        self._visible = tuple(
            row for row in self.snapshot.records
            if (not self._agent_filter or row.agent == self._agent_filter)
            and (not self._status_filter or row.status == self._status_filter))

    def _heading(self) -> str:
        filters = []
        if self._agent_filter:
            filters.append(f"agent={self._agent_filter}")
        if self._status_filter:
            filters.append(f"status={self._status_filter}")
        suffix = f" · {', '.join(filters)}" if filters else ""
        warnings = _warning_count(self.snapshot)
        warning = f" · {warnings} warning(s)" if warnings else ""
        history = ""
        if self._actor_history_index >= 0:
            group = self.snapshot.actor_history.groups[self._actor_history_index]
            history = f" · actor history {group.turn_id}"
        return (f"Agents ({len(self._visible)}/{len(self.snapshot.records)})"
                f"{suffix}{history}{warning}")

    def _options(self) -> list[Option]:
        return [Option(_record_label(row), id=f"agent-run-{index}")
                for index, row in enumerate(self._visible)]

    def _actor_tree(self) -> str:
        width = max(20, getattr(self.size, "width", 80) - 4)
        if self._actor_history_index < 0:
            if not self.snapshot.actor_tree.nodes and self.snapshot.actor_history.groups:
                return "\n".join(render_actor_history(
                    self.snapshot.actor_history, width))
            return "\n".join(render_actor_tree(self.snapshot.actor_tree, width))
        group = self.snapshot.actor_history.groups[self._actor_history_index]
        lines = [f"ACTOR HISTORY · turn {group.turn_id}"]
        lines.extend(render_actor_tree(group.tree, width)[1:])
        return "\n".join(lines)

    def _refresh_actor_tree(self) -> None:
        if self.is_mounted:
            self.query_one("#agent-runs-heading", Static).update(self._heading())
            self.query_one("#agent-runs-tree", Static).update(self._actor_tree())

    def _update_detail(self, index: Optional[int]) -> None:
        self.query_one("#agent-runs-detail", Static).update(self._detail(index))

    def _detail(self, index: Optional[int]) -> str:
        if index is None or index < 0 or index >= len(self._visible):
            if self.snapshot.error:
                return f"Shared ledger unavailable: {self.snapshot.error}"
            return "No agent runs match the current filters."
        detail = _record_detail(self._visible[index])
        diagnostic = _snapshot_diagnostic(self.snapshot)
        return f"{detail}\n{diagnostic}" if diagnostic else detail


class TUIAgentsMixin:
    """Connect the shared ledger and current coordinator turn to an overlay."""

    def _init_agents_inspector(self) -> None:
        self._agent_operation_started_ns = 0
        self._agent_operation_kind = ""
        self._agent_operation_task = ""

    def _slash_agents(self, args: tuple[str, ...] = ()) -> None:
        if len(args) > 1:
            self._usage("agents", "[agent]")
            return
        self._open_agents_inspector(args[0] if args else "")

    def _slash_runs(self, args: tuple[str, ...] = ()) -> None:
        self._slash_agents(args)

    def _palette_agents(self) -> None:
        self._open_agents_inspector("")

    def _open_agents_inspector(self, agent_filter: str) -> None:
        snapshot = self._agent_runs_snapshot()
        self.push_screen(TUIAgentRunsScreen(
            snapshot, self._agent_runs_snapshot, agent_filter=agent_filter))

    def _agent_runs_snapshot(self) -> AgentRunsSnapshot:
        from . import config
        cwd = str(getattr(self._backend, "cwd", "")
                  or self.options.cwd or "")
        ledger = query_snapshot(
            config.data_dir(cwd), limit=AGENT_RUN_DISPLAY_LIMIT)
        view = getattr(self, "_runtime_view", None)
        actor_tree = (getattr(view, "actors", ActorTreeSnapshot())
                      if getattr(self, "_projection_driven", False)
                      else ActorTreeSnapshot())
        actor_history = (getattr(view, "actor_history", ActorHistorySnapshot())
                         if getattr(self, "_projection_driven", False)
                         else ActorHistorySnapshot())
        return build_agent_runs_snapshot(
            ledger, self._current_agent_run(), actor_tree, actor_history)

    def _current_agent_run(self) -> Optional[AgentRunRecord]:
        if not self._busy or not self._agent_operation_started_ns:
            return None
        snapshot = self._backend_snapshot()
        if not isinstance(snapshot, dict):
            snapshot = {}
        if self._agent_operation_kind == "review":
            return self._review_agent_run(snapshot)
        view = getattr(self, "_runtime_view", None)
        projected = (str(getattr(view, "turn_state", "") or "").lower()
                     if getattr(self, "_projection_driven", False) else "")
        status = projected or "running"
        started = self._agent_operation_started_ns
        projected_run = (getattr(view, "run_id", "")
                         if getattr(self, "_projection_driven", False) else "")
        projected_session = (getattr(view, "session_id", "")
                             if getattr(self, "_projection_driven", False) else "")
        return AgentRunRecord(
            "coordinator", _iso_timestamp(started),
            _text(snapshot.get("agent") or self.options.agent or "agent", 64),
            _text(snapshot.get("provider") or self.options.provider, 128),
            status, _text(self._agent_operation_task, 512),
            elapsed_ms=max(0, (time.time_ns() - started) // 1_000_000),
            run_id=_text(projected_run, 160),
            session_id=_text(
                projected_session or snapshot.get("session_id"), 160),
            session_name=_text(snapshot.get("session_name"), 160),
            current=True)

    def _review_agent_run(self, snapshot: dict[str, Any]) -> AgentRunRecord:
        started = self._agent_operation_started_ns
        output = str(getattr(self, "_review_output", "") or "")
        return AgentRunRecord(
            "coordinator", _iso_timestamp(started), "review", "", "running",
            _text(self._agent_operation_task, 512),
            elapsed_ms=max(0, (time.time_ns() - started) // 1_000_000),
            session_id=_text(snapshot.get("session_id"), 160),
            session_name=_text(snapshot.get("session_name"), 160),
            outputs=(_text(output, 512),) if output else (), current=True)

    def _mark_agent_operation_started(self, kind: str, task: str) -> None:
        self._agent_operation_started_ns = time.time_ns()
        self._agent_operation_kind = _text(kind, 32)
        self._agent_operation_task = _text(task, 512)
        self._refresh_agents_inspector()

    def _mark_agent_operation_finished(self) -> None:
        self._agent_operation_started_ns = 0
        self._agent_operation_kind = ""
        self._agent_operation_task = ""
        self._refresh_agents_inspector()

    def _refresh_agents_inspector(self) -> None:
        if not self.is_mounted:
            return
        current = self.screen
        if isinstance(current, TUIAgentRunsScreen):
            current.refresh_snapshot(self._agent_runs_snapshot())


def _ledger_record(value: Any) -> Optional[AgentRunRecord]:
    if not isinstance(value, dict):
        return None
    agent = _text(value.get("agent"), 64)
    if not agent:
        return None
    outputs = value.get("outputs", ())
    safe_outputs = tuple(_text(item, 512) for item in
                         (outputs if isinstance(outputs, list) else ())[:8]
                         if _text(item, 512))
    return AgentRunRecord(
        "ledger", _text(value.get("ts"), 64), agent,
        _text(value.get("provider"), 128),
        _text(value.get("status"), 32).lower() or "unknown",
        _text(value.get("task_brief"), 512), _nonnegative(value.get("attempt")),
        _nonnegative(value.get("elapsed_ms")), _integer(value.get("exit_code")),
        _text(value.get("run_id"), 160), _text(value.get("session_id"), 160),
        _text(value.get("session_name"), 160), safe_outputs)


def _record_label(row: AgentRunRecord) -> str:
    marker = "●" if row.current else _status_marker(row.status)
    timestamp = row.timestamp.replace("T", " ").removesuffix("Z") or "-"
    task = _text(row.task_brief, 84) or "-"
    return (f"{marker} {row.agent} · {row.status} · "
            f"{_text(timestamp, 24)} · {task}")


def _record_detail(row: AgentRunRecord) -> str:
    exit_label = "pending" if row.current else str(row.exit_code)
    lines = [
        f"Source: {row.source}{' (current)' if row.current else ''}",
        f"Agent: {row.agent} · Provider: {row.provider or '-'} · Status: {row.status}",
        f"Time: {row.timestamp or '-'} · Attempt: {row.attempt} · "
        f"Elapsed: {_duration(row.elapsed_ms)} · Exit: {exit_label}",
        f"Session: {row.session_name or '-'} · {_text(row.session_id, 120) or '-'}",
        f"Run: {_text(row.run_id, 160) or '-'}",
        f"Task: {_text(row.task_brief, 600) or '-'}",
    ]
    if row.outputs:
        lines.append("Outputs: " + ", ".join(row.outputs))
    return _bound_detail("\n".join(lines))


def _warning_count(snapshot: AgentRunsSnapshot) -> int:
    history = snapshot.actor_history
    history_warnings = int(history.truncated or history.skipped)
    return (snapshot.skipped_records + int(snapshot.truncated)
            + int(bool(snapshot.error)) + history_warnings)


def _snapshot_diagnostic(snapshot: AgentRunsSnapshot) -> str:
    values = []
    if snapshot.skipped_records:
        values.append(f"{snapshot.skipped_records} unsafe record(s) skipped")
    if snapshot.truncated:
        values.append("newest bounded records shown")
    if snapshot.error:
        values.append(f"ledger error: {snapshot.error}")
    if snapshot.ledger_bytes:
        values.append(f"{snapshot.ledger_bytes} ledger byte(s) read")
    return "Ledger: " + " · ".join(values) if values else ""


def _next_filter(values: list[str], current: str) -> str:
    try:
        index = values.index(current)
    except ValueError:
        return ""
    return values[(index + 1) % len(values)] if values else ""


def _status_marker(status: str) -> str:
    return {"succeeded": "✓", "failed": "✗", "rejected": "!",
            "cancelled": "-", "interrupted": "-"}.get(status, "?")


def _duration(value: int) -> str:
    return f"{value / 1000:.2f}s" if value else "-"


def _iso_timestamp(value: int) -> str:
    try:
        stamp = datetime.fromtimestamp(value / 1_000_000_000, timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return ""
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _nonnegative(value: Any) -> int:
    return max(0, _integer(value))


def _text(value: Any, limit: int) -> str:
    return one_line(clean_text(value))[:max(0, limit)]


def _bound_detail(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _DETAIL_MAX_BYTES:
        return value
    return encoded[:_DETAIL_MAX_BYTES - 3].decode("utf-8", "ignore") + "…"
