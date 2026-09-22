"""Close confirmation, request recovery and shared remote state in the TUI."""

import asyncio
import threading

import pytest
from textual.widgets import Button, Select, Static, TextArea

from pbatch.hub_client import HubError
from pbatch.hub_session_close_tui import CloseConfirmation
from pbatch.hub_tui import HubTUI
from test_hub_session_close_client import record


class Client:
    def __init__(self):
        self.calls = []
        self.reads = 0
        self.status = "queued"
        self.error = None
        self.supported = True
        self.closed = False
        self.active = ""
        self.gate = None
        self.entered = threading.Event()

    def list_instances(self):
        return [{"instance_id": "alpha", "online": True, "session_close_supported": self.supported}]

    def list_sessions(self, _instance_id):
        return [self.get_session(name) for name in ("session-1", "session-2")]

    def get_session(self, session_id):
        closed = self.closed and session_id == "session-1"
        return dict(session_id=session_id, instance_id="alpha", name=session_id, online=True,
                    status="closed" if closed else "idle", controllable=not closed,
                    frozen=closed, active_turn_id=self.active)

    def session_events(self, _session_id, *, after="", **_kwargs):
        return [] if after else [{"cursor": 1, "kind": "message.completed",
                                   "payload": {"text": "Previous result remains available"}}]

    def close_session(self, session_id, *, idempotency_key):
        self.calls.append((session_id, idempotency_key))
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        if self.error:
            error, self.error = self.error, None
            raise error
        return record(status=self.status)

    def session_close_request(self, request_id):
        self.reads += 1
        assert request_id == "close-1"
        if self.error:
            error, self.error = self.error, None
            raise error
        if self.status == "closed":
            self.closed = True
        return record(status=self.status)


async def confirm(app, pilot):
    await pilot.click("#session-close")
    await pilot.pause()
    assert isinstance(app.screen, CloseConfirmation)
    await pilot.click("#close-confirm")
    await pilot.pause()


def test_close_requires_confirmation_and_preserves_history_after_success():
    async def scenario():
        client, app = Client(), None
        app = HubTUI(client)
        async with app.run_test(size=(100, 38)) as pilot:
            await pilot.pause()
            await pilot.click("#session-close")
            await pilot.pause()
            assert client.calls == []
            await pilot.click("#close-keep")
            await pilot.pause()
            assert client.calls == []
            await confirm(app, pilot)
            assert len(client.calls) == 1
            assert app.query_one("#send", Button).disabled
            client.status = "closed"
            await app._closer.poll()
            assert app._selected_session == "session-1"
            assert app.query_one("#composer", TextArea).disabled
            assert "capacity is available" in app.query_one("#close-status", Static).render().plain
            assert "Previous result remains" in app.query_one("#events", Static).render().plain
            assert len(client.calls) == 1 and client.reads >= 1
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["forbidden", "session_busy", "instance_offline"])
def test_unknown_close_keeps_target_and_key_after_a_rejected_retry(code):
    async def scenario():
        client = Client()
        client.error = HubError("PRIVATE lost ACK", code="hub_unavailable")
        app = HubTUI(client)
        async with app.run_test(size=(100, 38)) as pilot:
            await pilot.pause()
            await confirm(app, pilot)
            key = app._closer.pending["key"]
            app.query_one("#session", Select).value = "session-2"
            await pilot.pause()
            assert not app.query_one("#send", Button).disabled
            client.error = HubError("PRIVATE refusal", code=code, status=403 if code == "forbidden" else 409)
            await app._closer.retry()
            assert app._closer.pending["key"] == key
            await app._closer.retry()
            assert client.calls == [("session-1", key)] * 3
            assert app._selected_session == "session-2"
            assert "PRIVATE" not in app.query_one("#close-status", Static).render().plain
            await app._closer.poll()
            assert len(client.calls) == 3
    asyncio.run(scenario())


def test_definite_busy_rejection_releases_local_pending_workflow():
    async def scenario():
        client = Client()
        client.error = HubError("PRIVATE", code="session_busy", status=409)
        app = HubTUI(client)
        async with app.run_test(size=(100, 38)) as pilot:
            await pilot.pause()
            await confirm(app, pilot)
            assert app._closer.pending is None
            assert not app.query_one("#send", Button).disabled
            assert "active prompt or computation" in app.query_one("#close-status", Static).render().plain
            assert "PRIVATE" not in app.query_one("#close-status", Static).render().plain
    asyncio.run(scenario())


def test_failed_status_read_pauses_until_explicit_get_retry():
    async def scenario():
        client, app = Client(), None
        app = HubTUI(client)
        async with app.run_test(size=(100, 38)) as pilot:
            await pilot.pause()
            await confirm(app, pilot)
            client.error = HubError("PRIVATE", status=401)
            await app._closer.poll()
            reads = client.reads
            await app._closer.poll()
            assert client.reads == reads
            assert "fleet auth login" in app.query_one("#close-status", Static).render().plain
            await app._closer.retry()
            assert client.reads == reads + 1 and len(client.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("supported,active", [(False, ""), ("true", ""), (True, "turn-1")])
def test_legacy_or_active_session_has_no_close_action(supported, active):
    async def scenario():
        client = Client()
        client.supported, client.active = supported, active
        app = HubTUI(client)
        async with app.run_test(size=(100, 38)) as pilot:
            await pilot.pause()
            assert app.query_one("#session-close", Button).disabled
    asyncio.run(scenario())


def test_close_from_another_device_refreshes_controls_without_losing_history():
    async def scenario():
        client, app = Client(), None
        app = HubTUI(client)
        async with app.run_test(size=(100, 38)) as pilot:
            await pilot.pause()
            client.closed = True
            await app._closer.refresh_selected()
            assert app.query_one("#send", Button).disabled
            assert app._selected_session == "session-1"
            assert "Previous result remains" in app.query_one("#events", Static).render().plain
    asyncio.run(scenario())


def test_late_close_response_does_not_change_a_new_selection():
    async def scenario():
        client = Client()
        client.gate = threading.Event()
        client.status = "closed"
        app = HubTUI(client)
        async with app.run_test(size=(100, 38)) as pilot:
            await pilot.pause()
            app._closer.pending = dict(session_id="session-1", instance_id="alpha", key="fixed", record={})
            task = asyncio.create_task(app._closer._submit())
            try:
                assert await asyncio.to_thread(client.entered.wait, 2)
                app.query_one("#session", Select).value = "session-2"
                await pilot.pause()
                client.gate.set()
                await task
                assert app._selected_session == "session-2"
                assert not app.query_one("#send", Button).disabled
            finally:
                client.gate.set()
                await task
    asyncio.run(scenario())
