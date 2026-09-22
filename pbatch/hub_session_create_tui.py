"""Bounded creation-request tracking for the remote-session TUI."""

from __future__ import annotations

import asyncio
import uuid

from textual.widgets import Button, Input, Static

from .hub_auth_feedback import error_status
from .hub_session_client import creation_record, session_name


def supports_creation(instance: dict) -> bool:
    capacity = instance.get("session_capacity", 0)
    return instance.get("online") is True and type(capacity) is int and 1 <= capacity <= 8


class HubSessionCreation:
    def __init__(self, app):
        self.app = app
        self.pending = None
        self.busy = False
        self.polling = False

    def update_controls(self) -> None:
        instance = self.app._instances.get(self.app._selected_instance, {})
        queued = self.pending and self.pending.get("record", {}).get("status") == "queued"
        self.app.query_one("#session-create", Button).disabled = (
            self.busy or bool(queued) or not supports_creation(instance))
        self.app.query_one("#session-name", Input).disabled = self.busy or bool(queued)

    def _status(self, text: str) -> None:
        self.app.query_one("#creation-status", Static).update(text[:512])

    def _prepare(self) -> dict:
        instance_id = self.app._selected_instance
        if not supports_creation(self.app._instances.get(instance_id, {})):
            raise ValueError("Select an online instance that supports new sessions.")
        name = session_name(self.app.query_one("#session-name", Input).value)
        pending = self.pending
        if pending and pending.get("record", {}).get("status", "queued") == "queued":
            if (pending["instance_id"], pending["name"]) != (instance_id, name):
                raise ValueError("Resolve the unconfirmed creation before requesting another session.")
            return pending
        self.pending = {"instance_id": instance_id, "name": name, "key": uuid.uuid4().hex,
                        "selection": self.app._selection_generation, "record": {}}
        return self.pending

    async def submit(self) -> None:
        if self.busy:
            return
        try:
            pending = self._prepare()
        except ValueError as exc:
            self._status(str(exc))
            return
        if pending["record"].get("status") == "queued":
            await self.poll()
            return
        recovering = bool(pending.get("attempted"))
        pending["attempted"] = True
        self.busy = True
        self.update_controls()
        try:
            record = await asyncio.to_thread(
                self.app.client.create_session, pending["instance_id"], pending["name"],
                idempotency_key=pending["key"])
            await self._receive(pending, record)
        except Exception as exc:
            if not recovering and getattr(exc, "code", "") in {
                    "session_creation_unsupported", "session_capacity", "instance_offline", "not_found"}:
                self.pending = None
                self._status("Creation was not accepted; choose an available instance.")
            else:
                self._status(error_status("Creation unconfirmed; retry the same instance and name", exc))
        finally:
            self.busy = False
            self.update_controls()

    async def poll(self) -> None:
        pending = self.pending
        if self.busy or self.polling or not pending or pending["record"].get("status") != "queued":
            return
        self.polling = True
        try:
            record = await asyncio.to_thread(
                self.app.client.session_request, pending["record"]["request_id"])
            if self.pending is pending:
                await self._receive(pending, record)
        except Exception as exc:
            self._status(error_status("Creation status unavailable; the request is retained", exc))
        finally:
            self.polling = False
            self.update_controls()

    async def _receive(self, pending: dict, value: dict) -> None:
        record = creation_record(value, instance_id=pending["instance_id"], name=pending["name"],
                                 request_id=pending["record"].get("request_id", ""))
        pending["record"] = record
        state = record["status"]
        if state == "created":
            self._status(f"Session created: {record['session_id']}")
            await self.app._creation_ready(record, pending["selection"])
        elif state == "queued":
            self._status(f"Waiting for the instance to create the session: {record['request_id']}")
        elif state == "lost":
            self._status("The instance restarted before confirming creation. Request a new session.")
        else:
            self._status("The instance could not create the session. Check its capacity and runtime.")
