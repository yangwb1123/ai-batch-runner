"""Textual interface for session-bound device compute tasks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import PurePosixPath
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (Button, Footer, Header, Input, Select, Static,
                             TabbedContent, TabPane)

from .hub_compute_client import ComputeClient
from .hub_compute_models import resource_request
from . import hub_workspace_tui as workspace_ui
from .hub_compute_tui_views import (
    _id, _permission_error, _session_label, _device_label, _device_summary,
    _task_label, _task_detail, _gpu_label, _memory_label, _result_view,
    MAX_DEVICES_SHOWN, MAX_TASK_OUTPUT_CHARS)

MAX_PENDING_SUBMISSIONS = 128
MAX_TASKS_SHOWN = 200


class ComputeTUI(App[None]):
    """Browse sessions/devices and submit or control compute tasks."""

    TITLE = "pi-batch fleet compute"
    SUB_TITLE = "Session-bound remote tasks"
    BINDINGS = [("r", "refresh", "Refresh"), ("q", "quit", "Quit")]
    CSS = """
    #main { height: 1fr; }
    #context { height: 3; }
    #session { width: 1fr; }
    #target { width: 1fr; }
    #refresh { width: 14; }
    #devices { height: 4; border: round $accent; padding: 0 1; overflow-y: auto; }
    #tabs { height: 1fr; }
    #submit-form { height: 1fr; }
    .form-row { height: 3; }
    .form-input { width: 1fr; }
    #submit-row { height: 3; }
    #submit { width: 18; }
    #tasks-layout { height: 1fr; }
    #task-select { width: 36; }
    #task-detail { width: 1fr; height: 1fr; border: round $accent; padding: 1; overflow-y: auto; }
    #task-actions { height: 3; }
    #status { height: 1; }
    """

    def __init__(self, client: ComputeClient):
        super().__init__()
        self.client = client
        self._sessions: dict[str, dict[str, Any]] = {}
        self._devices: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._selected_session = ""
        self._selected_task = ""
        self._pending: dict[tuple[str, str], str] = {}
        self._submitting = False
        self._session_seq = 0
        self._device_seq = 0
        self._task_seq = 0
        self._detail_seq = 0
        self._workspace_seq = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main"):
            with Horizontal(id="context"):
                yield Select([], prompt="Select session", id="session")
                yield Select([], prompt="Automatic device", id="target")
                yield Button("Refresh", id="refresh")
            yield Static("Choose a session to load its project devices.",
                         id="devices", markup=False)
            with TabbedContent(id="tabs"):
                with TabPane("Submit", id="submit-pane"):
                    with VerticalScroll(id="submit-form"):
                        with Horizontal(classes="form-row"):
                            yield Input(value='["python", "job.py"]',
                                        placeholder='JSON argv, e.g. ["python", "job.py"]',
                                        id="argv", classes="form-input")
                        with Horizontal(classes="form-row"):
                            yield Input(value=".", placeholder="workdir below project root",
                                        id="workdir", classes="form-input")
                            yield Input(value="60", placeholder="timeout seconds",
                                        id="timeout", classes="form-input")
                        with Horizontal(classes="form-row"):
                            yield Input(value="1", placeholder="CPU cores",
                                        id="cpu-cores", classes="form-input")
                            yield Input(value="0", placeholder="memory MiB (0 = no minimum)",
                                        id="memory-mib", classes="form-input")
                        with Horizontal(classes="form-row"):
                            yield Input(value="0", placeholder="NVIDIA GPUs (0..16)",
                                        id="gpu-count", classes="form-input")
                            yield Input(value="0", placeholder="minimum MiB per GPU",
                                        id="gpu-memory-mib", classes="form-input")
                        yield from workspace_ui.controls()
                        with Horizontal(id="submit-row"):
                            yield Button("Submit compute task", id="submit", variant="primary")
                            yield Static("Select a controllable session.",
                                         id="submit-hint", markup=False)
                with TabPane("Tasks", id="tasks-pane"):
                    with Vertical(id="tasks-layout"):
                        with Horizontal():
                            yield Select([], prompt="Tasks for selected session",
                                         id="task-select")
                            yield Static("Select a task to view status and result.",
                                         id="task-detail", markup=False)
                        yield from workspace_ui.output_controls()
                        with Horizontal(id="task-actions"):
                            yield Button("Refresh tasks", id="refresh-tasks")
                            yield Button("Cancel selected", id="cancel-task", variant="error")
                            yield Static("", id="task-status", markup=False)
            yield Static("", id="status", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(5.0, self._refresh_selected)
        await self._refresh_sessions()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "session":
            workspace_ui.clear(self)
            self._selected_session = _selected_value(event.value)
            self._selected_task = ""
            self._clear_task_view()
            await self._refresh_selected()
        elif event.select.id == "target":
            self._update_controls()
        elif event.select.id == "task-select":
            self._selected_task = _selected_value(event.value)
            await self._load_task_detail(self._selected_task)
        self._update_controls()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            await self._refresh_sessions()
            await self._refresh_selected()
        elif event.button.id == "refresh-tasks":
            await self._refresh_tasks()
        elif event.button.id == "submit":
            await self._submit_task()
        elif event.button.id == "cancel-task":
            await self._cancel_task()
        elif event.button.id in {"workspace-upload", "workspace-download"}:
            await workspace_ui.handle(self, event.button.id)

    async def action_refresh(self) -> None:
        await self._refresh_sessions()
        await self._refresh_selected()

    async def _refresh_sessions(self) -> None:
        self._session_seq += 1
        request_seq = self._session_seq
        try:
            rows = await asyncio.to_thread(self.client.client.list_sessions)
        except Exception as exc:
            if request_seq == self._session_seq:
                self._sessions.clear()
                self._selected_session = ""
                self.query_one("#session", Select).set_options([])
                self._clear_selected_data()
                self._set_status(_permission_error("Session list", "agent.sessions:read", exc))
            return
        if request_seq != self._session_seq:
            return
        self._sessions = {_id(row, "session_id"): row for row in rows
                          if _id(row, "session_id")}
        options = [(_session_label(row), session_id)
                   for session_id, row in self._sessions.items()]
        selector = self.query_one("#session", Select)
        selector.set_options(options)
        if options and self._selected_session not in self._sessions:
            self._selected_session = options[0][1]
            selector.value = self._selected_session
        elif not options:
            self._selected_session = ""
            self._clear_selected_data()
            self._set_status("No sessions are available to this Hub identity.")
        self._update_controls()

    async def _refresh_selected(self) -> None:
        if not self._selected_session:
            return
        await asyncio.gather(self._refresh_devices(), self._refresh_tasks(), workspace_ui.refresh(self))

    async def _refresh_devices(self) -> None:
        session_id = self._selected_session
        project_id = _id(self._sessions.get(session_id, {}), "project_id")
        self._device_seq += 1
        request_seq = self._device_seq
        if not session_id:
            self._clear_devices("Select a session to load its project devices.")
            return
        try:
            rows = await asyncio.to_thread(self.client.devices, project_id)
        except Exception as exc:
            if session_id == self._selected_session and request_seq == self._device_seq:
                self._devices.clear()
                self._clear_devices(_permission_error(
                    "Device list", "agent.devices:read", exc))
                self._update_controls()
            return
        if session_id != self._selected_session or request_seq != self._device_seq:
            return
        self._devices = {_id(row, "device_id"): row for row in rows
                         if _id(row, "device_id")}
        options = [("Automatic placement", "")]
        options.extend((_device_label(row), device_id)
                       for device_id, row in self._devices.items()
                       if row.get("schedulable") is True)
        selector = self.query_one("#target", Select)
        selected = _selected_value(selector.value)
        selector.set_options(options)
        selector.value = selected if selected in {value for _, value in options} else ""
        self.query_one("#devices", Static).update(_device_summary(list(self._devices.values())))
        self._update_controls()

    async def _refresh_tasks(self) -> None:
        session_id = self._selected_session
        self._task_seq += 1
        request_seq = self._task_seq
        if not session_id:
            self._clear_task_view()
            return
        try:
            rows = await asyncio.to_thread(self.client.tasks, session_id)
        except Exception as exc:
            if session_id == self._selected_session and request_seq == self._task_seq:
                self._tasks.clear()
                self.query_one("#task-select", Select).set_options([])
                self.query_one("#task-detail", Static).update(
                    _permission_error("Task list", "agent.tasks:read", exc))
                self._set_task_status(_permission_error(
                    "Task list", "agent.tasks:read", exc))
            return
        if session_id != self._selected_session or request_seq != self._task_seq:
            return
        self._tasks = {_id(row, "task_id"): row for row in rows[:MAX_TASKS_SHOWN]
                       if _id(row, "task_id")}
        options = [(_task_label(row), task_id)
                   for task_id, row in self._tasks.items()]
        selector = self.query_one("#task-select", Select)
        selector.set_options(options)
        if options and self._selected_task not in self._tasks:
            self._selected_task = options[0][1]
            selector.value = self._selected_task
        elif not options:
            self._selected_task = ""
            self.query_one("#task-detail", Static).update("No compute tasks for this session.")
        await self._load_task_detail(self._selected_task)
        self._update_controls()

    async def _load_task_detail(self, task_id: str) -> None:
        self._detail_seq += 1
        request_seq = self._detail_seq
        if not task_id:
            self.query_one("#task-detail", Static).update("Select a task to view details.")
            return
        try:
            detail = await asyncio.to_thread(self.client.get, task_id)
        except Exception as exc:
            if task_id == self._selected_task and request_seq == self._detail_seq:
                error = _permission_error("Task detail", "agent.tasks:read", exc)
                self.query_one("#task-detail", Static).update(error)
                self._set_task_status(error)
            return
        if task_id != self._selected_task or request_seq != self._detail_seq:
            return
        self._tasks[task_id] = detail
        self.query_one("#task-detail", Static).update(_task_detail(detail))
        self._update_controls()

    async def _submit_task(self) -> None:
        session_id = self._selected_session
        session = self._sessions.get(session_id, {})
        if not session_id or session.get("controllable") is not True:
            self._set_task_status("Select an online, controllable session first.")
            return
        try:
            body = _request_body(self)
        except ValueError as exc:
            self._set_task_status(str(exc))
            return
        body["target_device_id"] = _selected_value(
            self.query_one("#target", Select).value)
        fingerprint = _body_fingerprint(body)
        pending_key = (session_id, fingerprint)
        idem = self._pending.get(pending_key)
        if idem is None and len(self._pending) >= MAX_PENDING_SUBMISSIONS:
            self._set_task_status("Too many unconfirmed requests; retry or restart the TUI.")
            return
        idem = idem or uuid.uuid4().hex
        self._pending[pending_key] = idem
        self._submitting = True
        self._update_controls()
        try:
            result = await asyncio.to_thread(self.client.submit, session_id, body, idem)
        except Exception as exc:
            self._set_task_status(_permission_error(
                "Submission unconfirmed; retry unchanged input to reuse its key",
                "agent.tasks:write", exc))
            return
        finally:
            self._submitting = False
            self._update_controls()
        task_id = _id(result, "task_id")
        self._pending.pop(pending_key, None)
        if session_id == self._selected_session:
            self._set_task_status(f"Task queued: {task_id or 'response received'}")
            if task_id:
                self._selected_task = task_id
                self.query_one("#tabs", TabbedContent).active = "tasks-pane"
            await self._refresh_tasks()

    async def _cancel_task(self) -> None:
        task_id = self._selected_task
        task = self._tasks.get(task_id, {})
        if not task_id or _id(task, "state") in {"completed", "failed", "cancelled", "lost"}:
            self._set_task_status("Select a non-terminal task to cancel.")
            return
        try:
            result = await asyncio.to_thread(self.client.cancel, task_id)
        except Exception as exc:
            self._set_task_status(_permission_error(
                "Cancel failed", "agent.tasks:cancel", exc))
            return
        self._set_task_status(f"Cancellation requested: {_id(result, 'state') or 'pending'}")
        await self._load_task_detail(task_id)
        await self._refresh_tasks()

    def _clear_selected_data(self) -> None:
        workspace_ui.clear(self)
        self._devices.clear()
        self._tasks.clear()
        self._selected_task = ""
        self._clear_devices("Select a session to load its project devices.")
        self._clear_task_view()

    def _clear_devices(self, message: str) -> None:
        self.query_one("#devices", Static).update(message)
        selector = self.query_one("#target", Select)
        selector.set_options([("Automatic placement", "")])
        selector.value = ""

    def _clear_task_view(self) -> None:
        self.query_one("#task-select", Select).set_options([])
        self.query_one("#task-detail", Static).update("Select a task to view details.")
        self._set_task_status("")

    def _update_controls(self) -> None:
        session = self._sessions.get(self._selected_session, {})
        enabled = (session.get("controllable") is True and bool(self._devices)
                   and not self._submitting)
        self.query_one("#submit", Button).disabled = not enabled
        self.query_one("#submit-hint", Static).update(
            "Ready; target may be automatic or pinned." if enabled else
            "Submission needs an online controllable session and readable devices.")
        task = self._tasks.get(self._selected_task, {})
        state = _id(task, "state")
        self.query_one("#cancel-task", Button).disabled = (
            not state or state in {"completed", "failed", "cancelled", "lost"})

    def _set_status(self, value: str) -> None:
        self.query_one("#status", Static).update(str(value)[:512])

    def _set_task_status(self, value: str) -> None:
        self.query_one("#task-status", Static).update(str(value)[:384])


def run_compute_tui(client: ComputeClient) -> int:
    ComputeTUI(client).run()
    return 0


def _request_body(app: ComputeTUI) -> dict[str, Any]:
    raw_argv = app.query_one("#argv", Input).value
    try:
        argv = json.loads(raw_argv)
    except (TypeError, ValueError) as exc:
        raise ValueError("argv must be valid JSON") from exc
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or not argv[0]):
        raise ValueError("argv must be a non-empty JSON array of strings (1..128)")
    if len(json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 16384:
        raise ValueError("argv JSON exceeds 16 KiB")
    workdir = app.query_one("#workdir", Input).value.strip()
    path = PurePosixPath(workdir or ".")
    if len(workdir) > 512:
        raise ValueError("workdir exceeds 512 characters")
    if path.is_absolute() or ".." in path.parts or "\\" in workdir or "\0" in workdir:
        raise ValueError("workdir must stay beneath the project root")
    timeout = _integer_input(app, "timeout", 1, 3600)
    resources = resource_request({
        "cpu_cores": _integer_input(app, "cpu-cores", 1, 256),
        "memory_bytes": _integer_input(app, "memory-mib", 0, 16 * 1024**2) * 1048576,
        "gpu_count": _integer_input(app, "gpu-count", 0, 16),
        "gpu_memory_bytes": _integer_input(app, "gpu-memory-mib", 0, 1024**2) * 1048576})
    return workspace_ui.request(app, {"argv": argv, "workdir": str(path), "timeout": timeout,
                                      "resources": resources})


def _integer_input(app: ComputeTUI, field: str, minimum: int, maximum: int) -> int:
    value = app.query_one(f"#{field}", Input).value.strip()
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return number


def _body_fingerprint(body: dict[str, Any]) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _selected_value(value: Any) -> str:
    return "" if value is Select.BLANK else str(value or "")


__all__ = ["ComputeTUI", "run_compute_tui"]
