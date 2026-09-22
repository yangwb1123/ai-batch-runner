"""Textual browser for remote sessions registered in the Agent Hub."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Select, Static, TextArea

from .hub_client import HubClient
from .hub_auth_feedback import error_status
from .hub_session_create_tui import HubSessionCreation
from .hub_session_close_tui import HubSessionClosing


_MAX_PENDING_PROMPTS = 128
_ALL_INSTANCES = "\x1fhub-all-instances"


class HubTUI(App[None]):
    """Read and control remote sessions; history remains read-only."""

    TITLE = "pi-batch fleet"
    SUB_TITLE = "Agent Session Hub"
    CSS = """
    #content { height: 1fr; }
    #selectors { height: 3; }
    #creation { height: 3; }
    #session-name { width: 1fr; }
    #creation-status { height: auto; max-height: 3; }
    #close-actions { height: 3; }
    #close-status { height: auto; max-height: 3; }
    #events { height: 1fr; border: round $accent; padding: 1; overflow-y: auto; }
    #composer { height: 7; border: round $accent; }
    #actions { height: 3; }
    #status { height: 1; }
    """

    def __init__(self, client: HubClient):
        super().__init__()
        self.client = client
        self._instances: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._selected_instance = ""
        self._selected_session = ""
        self._pending_prompts: dict[str, tuple[str, str]] = {}
        self._submitting = False
        self._selection_generation = 0
        self._creator = HubSessionCreation(self)
        self._closer = HubSessionClosing(self)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="content"):
            with Horizontal(id="selectors"):
                yield Select([], prompt="Instance", id="instance")
                yield Select([], prompt="Session", id="session")
            with Horizontal(id="creation"):
                yield Input(placeholder="New session name", id="session-name")
                yield Button("New session", id="session-create")
            yield Static("", id="creation-status", markup=False)
            with Horizontal(id="close-actions"):
                yield Button("Close session", id="session-close", variant="error")
                yield Button("Retry close", id="close-retry")
                yield Button("Session requests", id="session-requests")
            yield Static("", id="close-status", markup=False)
            yield Static("Connect to the Hub to view sessions.", id="events", markup=False)
            yield TextArea(placeholder="Send a prompt to the selected session…",
                           id="composer")
            with Horizontal(id="actions"):
                yield Button("Refresh", id="refresh")
                yield Button("Send prompt", id="send", variant="primary")
                yield Button("Cancel turn", id="cancel", variant="error")
                yield Static("", id="status", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(2.0, self._refresh_events)
        self.set_interval(2.0, self._creator.poll)
        self.set_interval(2.0, self._closer.poll)
        self.set_interval(2.0, self._closer.refresh_selected)
        await self._refresh_instances()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "instance":
            if self._selected_instance != _selected_value(event.value):
                self._selection_generation += 1
            self._selected_instance = _selected_value(event.value)
            await self._refresh_sessions()
        elif event.select.id == "session":
            if self._selected_session != _selected_value(event.value):
                self._selection_generation += 1
            self._selected_session = _selected_value(event.value)
            if self._selected_session in self._sessions:
                selected = self._sessions[self._selected_session]
                selected.pop("_last_cursor", None)
                selected["_event_lines"] = []
            await self._refresh_events()
        self._update_controls()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            await self._refresh_instances()
            await self._refresh_sessions()
        elif event.button.id == "send":
            await self._send_prompt()
        elif event.button.id == "cancel":
            await self._cancel_turn()
        elif event.button.id == "session-create":
            await self._creator.submit()
        elif event.button.id == "session-close":
            self._closer.request()
        elif event.button.id == "close-retry":
            await self._closer.retry()
        elif (event.button.id == "session-requests" and self._selected_instance
              and self._selected_instance != _ALL_INSTANCES):
            from .hub_session_operations_tui import SessionOperations
            self.push_screen(SessionOperations(self, self._selected_instance))

    async def _refresh_instances(self) -> None:
        try:
            rows = await asyncio.to_thread(self.client.list_instances)
        except Exception as exc:
            self._instances.clear()
            self._selected_instance = ""
            self._clear_sessions()
            self._set_status(error_status("Hub unavailable", exc))
            return
        self._instances = {_identity(row, "instance_id"): row for row in rows
                           if _identity(row, "instance_id")}
        options = [("All instances", _ALL_INSTANCES)]
        options.extend((_instance_label(row), key) for key, row in self._instances.items())
        selector = self.query_one("#instance", Select)
        selector.set_options(options)
        if self._selected_instance not in self._instances and self._selected_instance != _ALL_INSTANCES:
            self._selected_instance = next(iter(self._instances), "")
        if self._selected_instance:
            selector.value = self._selected_instance
        elif not self._instances:
            selector.set_options([])
            self._selected_instance = ""
            self._clear_sessions()
        self._update_controls()

    async def _refresh_sessions(self) -> None:
        instance_id = self._selected_instance
        if not instance_id:
            self._clear_sessions()
            return
        query_instance = "" if instance_id == _ALL_INSTANCES else instance_id
        try:
            rows = await asyncio.to_thread(
                self.client.list_sessions, query_instance)
        except Exception as exc:
            self._clear_sessions()
            self._set_status(error_status("Session list unavailable", exc))
            return
        if instance_id != self._selected_instance:
            return
        self._sessions = {_identity(row, "session_id"): row for row in rows
                          if _identity(row, "session_id")}
        options = [_session_option(row, instance_id == _ALL_INSTANCES)
                   for key, row in self._sessions.items()]
        selector = self.query_one("#session", Select)
        selector.set_options(options)
        if options and self._selected_session not in self._sessions:
            self._selected_session = options[0][1]
            selector.value = self._selected_session
        elif not options:
            self._selected_session = ""
        await self._refresh_events()

    async def _creation_ready(self, record: dict, selection: int) -> None:
        if (selection != self._selection_generation
                or record["instance_id"] != self._selected_instance):
            return
        session_id = record["session_id"]
        try:
            session = await asyncio.to_thread(self.client.get_session, session_id)
        except Exception as exc:
            self._set_status(error_status("Session created; refresh its instance to open it", exc))
            return
        if selection != self._selection_generation or record["instance_id"] != self._selected_instance:
            return
        if session.get("session_id") != session_id or session.get("instance_id") != self._selected_instance:
            self._set_status("Session created; its details could not be verified.")
            return
        self._sessions[session_id] = session
        selector = self.query_one("#session", Select)
        selector.set_options([_session_option(row, self._selected_instance == _ALL_INSTANCES)
                              for key, row in self._sessions.items()])
        self._selected_session = session_id
        selector.value = session_id
        self._update_controls()
        await self._refresh_events()

    def _clear_sessions(self) -> None:
        self._sessions.clear()
        self._selected_session = ""
        self.query_one("#session", Select).set_options([])
        self.query_one("#events", Static).update("Select a session.")
        self._update_controls()

    async def _refresh_events(self) -> None:
        session_id = self._selected_session
        if not session_id:
            self.query_one("#events", Static).update("Select a session.")
            return
        try:
            selected = self._sessions.get(session_id, {})
            after = str(selected.get("_last_cursor", ""))
            events = await asyncio.to_thread(
                self.client.session_events, session_id,
                after=after, limit=100)
        except Exception as exc:
            self._set_status(error_status("Event history unavailable", exc))
            return
        if session_id != self._selected_session:
            return
        prior = list(selected.get("_event_lines", []))
        prior.extend(_event_line(event) for event in events)
        selected["_event_lines"] = prior[-60:]
        if events:
            selected["_last_cursor"] = _identity(events[-1], "cursor")
        text = "\n".join(selected["_event_lines"])
        self.query_one("#events", Static).update(text or "No events yet.")
        self._update_controls()

    async def _send_prompt(self) -> None:
        session_id = self._selected_session
        selected = self._sessions.get(session_id, {})
        prompt = self.query_one("#composer", TextArea).text.strip()
        if (self._submitting or not prompt or selected.get("controllable") is not True
                or self._closer.blocks(session_id)):
            self._set_status("Select a controllable session and enter a prompt.")
            return
        pending = self._pending_prompts.get(session_id)
        if pending is None and len(self._pending_prompts) >= _MAX_PENDING_PROMPTS:
            self._set_status("Too many unconfirmed submissions; retry an existing session first.")
            return
        if pending is None or pending[0] != prompt:
            pending = (prompt, uuid.uuid4().hex)
            self._pending_prompts.pop(session_id, None)
            self._pending_prompts[session_id] = pending
        self._submitting = True
        self._update_controls()
        try:
            result = await asyncio.to_thread(
                self.client.send_prompt, session_id, prompt, pending[1])
        except Exception as exc:
            self._set_status(error_status("Submission unconfirmed; retry keeps its request key", exc))
            return
        finally:
            self._submitting = False
            self._update_controls()
        self._pending_prompts.pop(session_id, None)
        composer = self.query_one("#composer", TextArea)
        if session_id == self._selected_session and composer.text.strip() == prompt:
            composer.text = ""
        self._set_status(f"Turn queued: {_identity(result, 'turn_id')}")
        await self._refresh_events()

    async def _cancel_turn(self) -> None:
        session_id = self._selected_session
        if not session_id:
            return
        try:
            details = await asyncio.to_thread(self.client.get_session, session_id)
            turn_id = (_identity(details, "active_turn_id") or
                       _identity(details, "turn_id"))
            if not turn_id:
                self._set_status("The selected session has no active turn.")
                return
            result = await asyncio.to_thread(self.client.cancel_turn, turn_id)
        except Exception as exc:
            self._set_status(error_status("Cancel request failed", exc))
            return
        self._set_status(f"Cancel requested: {_identity(result, 'turn_id') or turn_id}")

    def _update_controls(self) -> None:
        row = self._sessions.get(self._selected_session, {})
        can_control = row.get("controllable") is True and not self._closer.blocks(self._selected_session)
        self.query_one("#send", Button).disabled = not can_control or self._submitting
        self.query_one("#composer", TextArea).disabled = not can_control
        self._creator.update_controls()
        self._closer.update_controls()
        self.query_one("#session-requests", Button).disabled = (
            not self._selected_instance or self._selected_instance == _ALL_INSTANCES)

    def _set_status(self, value: str) -> None:
        self.query_one("#status", Static).update(value[:256])


def run_hub_tui(client: HubClient) -> int:
    HubTUI(client).run()
    return 0


def _selected_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _identity(row: dict[str, Any], key: str) -> str:
    value = row.get(key, "")
    return str(value) if isinstance(value, (str, int)) else ""


def _instance_label(row: dict[str, Any]) -> str:
    online = row.get("online")
    status = ("online" if online is True else "offline"
              if online is False else _identity(row, "status"))
    return f"{_identity(row, 'name') or 'instance'} · {status}"


def _session_label(row: dict[str, Any]) -> str:
    access = "read only" if row.get("controllable") is not True else "control"
    return f"{_identity(row, 'name') or 'session'} · {_identity(row, 'status')} · {access}"


def _session_option(row: dict[str, Any], include_instance: bool):
    label = _session_label(row)
    if include_instance:
        label = f"{_identity(row, 'instance_id') or 'instance'} · {label}"
    return label, _identity(row, "session_id")


def _event_line(event: dict[str, Any]) -> str:
    payload = event.get("payload", {})
    body = payload.get("text", "") if isinstance(payload, dict) else ""
    if not body and isinstance(payload, dict):
        body = payload.get("reason", "")
    raw_body = str(body).replace("\x00", "")
    safe_body = raw_body[:8192]
    if len(raw_body) > 8192 or (isinstance(payload, dict) and payload.get("truncated")):
        safe_body += " [truncated]"
    return f"{_identity(event, 'cursor')} {_identity(event, 'kind')} {safe_body}".rstrip()


__all__ = ["HubTUI", "run_hub_tui"]
