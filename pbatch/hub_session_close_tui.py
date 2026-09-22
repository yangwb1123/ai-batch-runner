"""Confirmed close requests, bounded recovery and cross-device state refresh."""

import asyncio
import uuid

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .hub_session_close_client import ADMISSION_ERRORS, close_error, close_record


class CloseConfirmation(ModalScreen[bool]):
    DEFAULT_CSS = """
    CloseConfirmation { align: center middle; }
    #close-confirmation { width: 70; max-width: 95%; height: auto; padding: 1 2;
                          border: round $warning; background: $surface; }
    #close-confirm-actions { height: 3; align-horizontal: right; }
    """

    def __init__(self, name):
        super().__init__()
        self.session_name = name

    def compose(self) -> ComposeResult:
        with Vertical(id="close-confirmation"):
            yield Static(f"Close session: {self.session_name}?\n"
                         "History is kept. This session cannot accept more prompts.\n"
                         "Active prompts or computations must finish first.", markup=False)
            with Horizontal(id="close-confirm-actions"):
                yield Button("Keep session", id="close-keep")
                yield Button("Close session", id="close-confirm", variant="error")

    def on_mount(self):
        self.query_one("#close-keep", Button).focus()

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "close-confirm")

    def key_escape(self):
        self.dismiss(False)


def supports_close(instance, session):
    return (instance.get("session_close_supported") is True
            and instance.get("online") is True and session.get("online") is True
            and session.get("controllable") is True and not session.get("frozen")
            and not session.get("active_turn_id"))


class HubSessionClosing:
    def __init__(self, app):
        self.app = app
        self.pending = None
        self.busy = False
        self.polling = False
        self.paused = False
        self.refreshing = False
        self.confirming = False

    def unresolved(self):
        return self.pending and self.pending["record"].get("status", "queued") == "queued"

    def blocks(self, session_id):
        return bool(self.pending and self.pending["session_id"] == session_id)

    def update_controls(self):
        session = self.app._sessions.get(self.app._selected_session, {})
        instance = self.app._instances.get(self.app._selected_instance, {})
        self.app.query_one("#session-close", Button).disabled = bool(
            self.busy or self.confirming or self.unresolved() or self.app._submitting
            or not supports_close(instance, session))
        self.app.query_one("#close-retry", Button).disabled = bool(
            self.busy or self.polling or not self.unresolved())

    def _status(self, text):
        self.app.query_one("#close-status", Static).update(text[:512])

    def request(self):
        session_id = self.app._selected_session
        row = self.app._sessions.get(session_id, {})
        instance = self.app._instances.get(self.app._selected_instance, {})
        if self.confirming or self.unresolved() or not supports_close(instance, row):
            return
        pending = {"session_id": session_id, "instance_id": self.app._selected_instance,
                   "key": uuid.uuid4().hex, "record": {}}
        self.confirming = True
        self.update_controls()

        async def confirmed(accepted):
            self.confirming = False
            if accepted:
                self.pending = pending
                await self._submit()
            self.update_controls()

        self.app.push_screen(CloseConfirmation(row.get("name") or session_id), confirmed)

    async def retry(self):
        if self.busy or not self.unresolved():
            return
        self.paused = False
        if self.pending["record"].get("status") == "queued":
            await self.poll()
        else:
            await self._submit()

    async def _submit(self):
        pending = self.pending
        if self.busy or not pending:
            return
        recovering = bool(pending.get("attempted"))
        pending["attempted"] = True
        self.busy = True
        self.app._update_controls()
        try:
            value = await asyncio.to_thread(self.app.client.close_session, pending["session_id"],
                                            idempotency_key=pending["key"])
            self._receive(pending, value)
        except Exception as exc:
            if not recovering and getattr(exc, "code", "") in ADMISSION_ERRORS:
                self.pending = None
                self._status(close_error("Close was not accepted", exc))
            else:
                self._status(close_error("Close unconfirmed; Retry close keeps the original request", exc))
        finally:
            self.busy = False
            self.app._update_controls()

    async def poll(self):
        pending = self.pending
        if (self.busy or self.polling or self.paused or not pending
                or pending["record"].get("status") != "queued"):
            return
        self.polling = True
        try:
            value = await asyncio.to_thread(self.app.client.session_close_request,
                                            pending["record"]["request_id"])
            if self.pending is pending:
                self._receive(pending, value)
        except Exception as exc:
            self.paused = True
            self._status(close_error("Close status unavailable; Retry close checks the same request", exc))
        finally:
            self.polling = False
            self.app._update_controls()

    def _receive(self, pending, value):
        record = close_record(value, session_id=pending["session_id"], instance_id=pending["instance_id"],
                              request_id=pending["record"].get("request_id", ""))
        pending["record"] = record
        state = record["status"]
        row = self.app._sessions.get(record["session_id"])
        if row is not None:
            row.update(controllable=False, frozen=True,
                       status={"queued": "closing", "closed": "closed", "failed": "close_failed",
                               "lost": "close_lost"}[state])
        messages = {"queued": "Waiting for the instance to close the session; capacity remains reserved.",
                    "closed": "Session closed. History is kept and its capacity is available.",
                    "failed": "Close failed. History is kept; restart the Gateway to recover its capacity.",
                    "lost": "The instance restarted before confirming close. The old session is read-only."}
        self._status(f"{messages[state]} Request: {record['request_id']}")

    async def refresh_selected(self):
        session_id, instance_id = self.app._selected_session, self.app._selected_instance
        selection = self.app._selection_generation
        instance = self.app._instances.get(instance_id, {})
        if self.refreshing or not session_id or instance.get("session_close_supported") is not True:
            return
        self.refreshing = True
        try:
            details = await asyncio.to_thread(self.app.client.get_session, session_id)
            if selection != self.app._selection_generation or session_id != self.app._selected_session:
                return
            if details.get("session_id") != session_id or details.get("instance_id") != instance_id:
                raise ValueError("session response identity changed")
            row = self.app._sessions.get(session_id)
            if row is not None:
                row.update({key: details[key] for key in (
                    "status", "online", "controllable", "frozen", "active_turn_id") if key in details})
        except Exception as exc:
            if selection == self.app._selection_generation and session_id == self.app._selected_session:
                self._status(close_error("Session state unavailable; refresh to check remote changes", exc))
        finally:
            self.refreshing = False
            self.app._update_controls()
