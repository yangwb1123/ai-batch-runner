"""Textual browser for remote sessions registered in the Agent Hub."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Select, Static, TextArea

from .hub_client import HubClient


_MAX_PENDING_PROMPTS = 128


class HubTUI(App[None]):
    """Read and control remote sessions; history remains read-only."""

    TITLE = "pi-batch fleet"
    SUB_TITLE = "Agent Session Hub"
    CSS = """
    #content { height: 1fr; }
    #selectors { height: 3; }
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

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="content"):
            with Horizontal(id="selectors"):
                yield Select([], prompt="Instance", id="instance")
                yield Select([], prompt="Session", id="session")
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
        await self._refresh_instances()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "instance":
            self._selected_instance = _selected_value(event.value)
            await self._refresh_sessions()
        elif event.select.id == "session":
            self._selected_session = _selected_value(event.value)
            if self._selected_session:
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

    async def _refresh_instances(self) -> None:
        try:
            rows = await asyncio.to_thread(self.client.list_instances)
        except Exception as exc:
            self._instances.clear()
            self._selected_instance = ""
            self._clear_sessions()
            self._set_status(f"Hub unavailable ({type(exc).__name__})")
            return
        self._instances = {_identity(row, "instance_id"): row for row in rows
                           if _identity(row, "instance_id")}
        options = [(_instance_label(row), key) for key, row in self._instances.items()]
        selector = self.query_one("#instance", Select)
        selector.set_options(options)
        if options and self._selected_instance not in self._instances:
            self._selected_instance = options[0][1]
            selector.value = self._selected_instance
        elif not options:
            self._selected_instance = ""
            self._clear_sessions()

    async def _refresh_sessions(self) -> None:
        instance_id = self._selected_instance
        if not instance_id:
            self._clear_sessions()
            return
        try:
            rows = await asyncio.to_thread(
                self.client.list_sessions, instance_id)
        except Exception as exc:
            self._clear_sessions()
            self._set_status(f"Session list unavailable ({type(exc).__name__})")
            return
        if instance_id != self._selected_instance:
            return
        self._sessions = {_identity(row, "session_id"): row for row in rows
                          if _identity(row, "session_id")}
        options = [(_session_label(row), key) for key, row in self._sessions.items()]
        selector = self.query_one("#session", Select)
        selector.set_options(options)
        if options and self._selected_session not in self._sessions:
            self._selected_session = options[0][1]
            selector.value = self._selected_session
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
            self._set_status(f"Event history unavailable ({type(exc).__name__})")
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
        if self._submitting or not prompt or selected.get("controllable") is not True:
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
            self._set_status(f"Submission unconfirmed; retry keeps its request key ({type(exc).__name__})")
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
            self._set_status(f"Cancel request failed ({type(exc).__name__})")
            return
        self._set_status(f"Cancel requested: {_identity(result, 'turn_id') or turn_id}")

    def _update_controls(self) -> None:
        row = self._sessions.get(self._selected_session, {})
        can_control = row.get("controllable") is True
        self.query_one("#send", Button).disabled = not can_control or self._submitting
        self.query_one("#composer", TextArea).disabled = not can_control

    def _set_status(self, value: str) -> None:
        self.query_one("#status", Static).update(value[:256])


def run_hub_tui(client: HubClient) -> int:
    HubTUI(client).run()
    return 0


def _selected_value(value: Any) -> str:
    return "" if value is Select.BLANK else str(value or "")


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
