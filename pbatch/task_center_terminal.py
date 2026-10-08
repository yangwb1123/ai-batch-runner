"""Standalone read-only local task-center TUI. Independent of Agent/Hub/native TUI controllers."""
from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Input, Select, Static

from .task_center import snapshot
from .task_center_terminal_view import DISPLAY_KINDS, TerminalView, plain, record_facts, row_cells, validate_options


class TaskCenterApp(App):
    TITLE = "任务中心 · 只读"
    NOTICE = "只读本地账本；不是 Agent 会话或多设备控制台。活动/成功不等于进展/验收；无执行、清场、上传权。"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("q,ctrl+q,ctrl+c", "quit", "退出看板"), Binding("r,ctrl+r", "refresh", "刷新")]
    CSS = """
    Screen { layout: vertical; }
    #notice { height: 2; color: $text-muted; }
    #filters { height: 3; }
    #source-filter { width: 2fr; }
    #kind-filter { width: 1fr; }
    #records { height: 1fr; min-height: 4; }
    #summary, #diagnostics { height: auto; max-height: 4; }
    #details-scroll { height: 1fr; min-height: 4; border: solid $primary; }
    #record-facts, #record-detail { height: auto; }
    """

    def __init__(self, *, limit: int = 100, source: str = "", task_id: str = "", interval: float = 5.0, loader=None):
        validate_options(limit, interval, task_id)
        super().__init__()
        self.limit, self.interval = limit, interval
        self.model = TerminalView(source, task_id)
        self.loader = snapshot if loader is None else loader
        self.category = "all"
        self._view_reading = self._view_queued = self._view_closed = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self.NOTICE, id="notice", markup=False)
        with Horizontal(id="filters"):
            yield Input(self.model.source, placeholder="来源完整 ID/登记名；空=已接入来源", max_length=256, id="source-filter")
            yield Select([("全部记录", "all"), *[(spec[0], kind) for kind, spec in DISPLAY_KINDS.items()]],
                         value="all", allow_blank=False, id="kind-filter")
        yield Static("等待有界快照", id="summary", markup=False)
        yield DataTable(id="records", cursor_type="row", zebra_stripes=True)
        yield Static("", id="diagnostics", markup=False)
        with VerticalScroll(id="details-scroll"):
            yield Static("", id="record-facts", markup=False)
            yield Static(self.model.message, id="record-detail", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#records", DataTable).add_columns("项目", "类型", "完整身份", "观测状态", "摘要")
        self.set_interval(self.interval, self.request_refresh)
        self.request_refresh()
        self.query_one("#records", DataTable).focus()

    def action_refresh(self) -> None:
        self.request_refresh(force=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "source-filter":
            self.request_refresh(source=event.value, force=True)

    def on_select_changed(self, event: Select.Changed) -> None:
        if (event.select.id == "kind-filter" and event.value != self.category and
                (event.value == "all" or event.value in DISPLAY_KINDS)):
            self.category, self.model.selected = event.value, ""
            self._paint_records()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "records":
            self.model.select(event.row_key.value or "")
            self._paint_detail()

    def request_refresh(self, *, source: str | None = None, force: bool = False) -> None:
        if self._view_closed:
            return
        if self._view_reading and not force and source is None:
            return  # timer never piles up readers or worker threads
        generation = self.model.invalidate(source)
        self._paint_records()
        if self._view_reading:
            self._view_queued = True  # one pending latest scope; never cancel/restart a filesystem reader
            return
        self._view_reading = True
        self.run_worker(self._read_one(generation, self.model.source), name="read-only-task-snapshot", exit_on_error=False)

    async def _read_one(self, generation: int, source: str) -> None:
        try:
            value = await asyncio.to_thread(self.loader, self.limit, source)
            if not self._view_closed:
                self.model.publish(generation, value)
        except Exception as exc:
            if not self._view_closed:
                self.model.fail(generation, exc)
        finally:
            self._view_reading = False
            if not self._view_closed:
                self._paint_records()
                if self._view_queued:
                    self._view_queued = False
                    self.request_refresh(force=True)

    def _paint_records(self) -> None:
        table = self.query_one("#records", DataTable)
        table.clear()
        for key, record in self.model.items.items():
            if self.category == "all" or key.startswith(self.category + ":"):
                table.add_row(*(Text(cell) for cell in row_cells(key, record)), key=key)
        view = self.model.snapshot
        summary = ("窗口任务观测 " + str(view.get("observed_task_count", "未追踪")) +
                   "；遗漏任务 " + str(view.get("omitted_tasks", "未追踪")) + "；记录显示 " + str(table.row_count) +
                   "；关注事实 " + str((view.get("attention") or {}).get("observed_items", "未追踪"))
                   if view is not None else self.model.message)
        self.query_one("#summary", Static).update(Text(plain(summary, 512)))
        self.query_one("#diagnostics", Static).update(Text(self._diagnostics()))
        self._paint_detail()

    def _diagnostics(self) -> str:
        view = self.model.snapshot
        if view is None:
            return ""
        notes = list(view.get("diagnostics", []))
        for source in view.get("sources", []):
            for feed in source.get("coverage", []):
                if any(feed.get(key) for key in ("error", "malformed", "truncated", "incomplete")):
                    notes.append(plain(source.get("name"), 80) + ": 账本不完整或不可读；未知不是零")
        notes.extend(view.get("activity", {}).get("diagnostics", []))
        shown = [plain(item, 256) for item in notes[:8]]
        if len(notes) > 8:
            shown.append("另有 " + str(len(notes) - 8) + " 条诊断未显示")
        return "；".join(shown)

    def _paint_detail(self) -> None:
        record = self.model.items.get(self.model.selected)
        facts = record_facts(self.model.selected, record) if record is not None else ""
        self.query_one("#record-facts", Static).update(Text(facts))
        self.query_one("#record-detail", Static).update(Text(self.model.detail()))

    def action_quit(self) -> None:
        self.on_unmount()
        self.exit()

    def on_unmount(self) -> None:
        self._view_closed, self._view_queued = True, False
        self.model.invalidate()  # late results cannot restore rows or trigger new reads after exit


def run_terminal(limit: int, source: str, task_id: str, interval: float) -> int:
    TaskCenterApp(limit=limit, source=source, task_id=task_id, interval=interval).run()
    return 0  # exiting a viewer is never a task PASS, repair readiness or process cancellation
