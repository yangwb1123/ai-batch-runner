import asyncio
import threading

import pytest
from textual.widgets import Button, Input, Select, Static

from pbatch.hub_client import HubError
from pbatch.hub_tui import HubTUI


class Client:
    def __init__(self):
        self.calls = []
        self.state = "queued"
        self.fail = None
        self.gate = None
        self.started = threading.Event()

    def list_instances(self):
        return [{"instance_id": value, "name": value, "online": True, "session_capacity": 8}
                for value in ("alpha", "beta")]

    def list_sessions(self, instance_id):
        if self.state == "created" and instance_id == "alpha":
            return [self.get_session("new-session")]
        return []

    def session_events(self, *_a, **_k):
        return []

    def create_session(self, instance_id, name, *, idempotency_key):
        self.calls.append((instance_id, name, idempotency_key))
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        if self.fail is not None:
            failure, self.fail = self.fail, None
            raise failure
        return self.session_request("request-1")

    def session_request(self, request_id):
        assert request_id == "request-1"
        return {"request_id": request_id, "instance_id": "alpha", "name": "Analysis",
                "status": self.state, "session_id": "new-session" if self.state == "created" else "",
                "error_code": "owner_replaced" if self.state == "lost" else "",
                "created_at": 1, "updated_at": 2}

    def get_session(self, session_id):
        return {"session_id": session_id, "instance_id": "alpha", "name": "Analysis",
                "status": "idle", "controllable": True}


def test_tui_can_create_on_empty_instance_and_select_only_after_confirmation():
    async def scenario():
        client = Client()
        app = HubTUI(client)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            assert app._sessions == {}
            assert not app.query_one("#session-create", Button).disabled
            app.query_one("#session-name", Input).value = "Analysis"
            await pilot.click("#session-create")
            await pilot.pause()
            assert len(client.calls) == 1
            assert app._selected_session == ""
            assert app.query_one("#session-create", Button).disabled
            client.state = "created"
            await app._creator.poll()
            await pilot.pause()
            assert app._selected_session == "new-session"
            assert not app.query_one("#send", Button).disabled
            assert "Session created" in app.query_one("#creation-status", Static).render().plain
    asyncio.run(scenario())


@pytest.mark.parametrize("code,status", [("hub_unavailable", 0), ("unauthorized", 401)])
def test_tui_uncertain_creation_retry_keeps_key_and_hides_raw_error(code, status):
    async def scenario():
        client = Client()
        client.fail = HubError("SECRET upstream body", code=code, status=status)
        app = HubTUI(client)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.query_one("#session-name", Input).value = "Analysis"
            await app._creator.submit()
            text = app.query_one("#creation-status", Static).render().plain
            assert "SECRET" not in text and "unconfirmed" in text
            if status == 401:
                assert "fleet auth login" in text
            await app._creator.submit()
            assert len(client.calls) == 2
            assert client.calls[0] == client.calls[1]
            assert client.calls[0][2]
            await app._creator.poll()
            assert len(client.calls) == 2  # Poll never replays a business POST.
    asyncio.run(scenario())


def test_tui_does_not_change_request_identity_while_creation_is_uncertain():
    async def scenario():
        client = Client()
        client.fail = HubError("lost", code="hub_unavailable")
        app = HubTUI(client)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.query_one("#session-name", Input).value = "Analysis"
            await app._creator.submit()
            app.query_one("#session-name", Input).value = "Different task"
            await app._creator.submit()
            assert len(client.calls) == 1
            assert "Resolve the unconfirmed" in app.query_one("#creation-status", Static).render().plain
    asyncio.run(scenario())


def test_tui_late_creation_does_not_take_over_another_selected_instance():
    async def scenario():
        client = Client()
        client.gate = threading.Event()
        client.state = "created"
        app = HubTUI(client)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.query_one("#session-name", Input).value = "Analysis"
            operation = asyncio.create_task(app._creator.submit())
            try:
                assert await asyncio.to_thread(client.started.wait, 2)
                app.query_one("#instance", Select).value = "beta"
                await pilot.pause()
                client.gate.set()
                await operation
                assert app._selected_instance == "beta"
                assert app._selected_session == ""
            finally:
                client.gate.set()
                await operation
    asyncio.run(scenario())


def test_tui_known_admission_failure_allows_a_different_request():
    async def scenario():
        client = Client()
        client.fail = HubError("full", code="session_capacity", status=409)
        app = HubTUI(client)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.query_one("#session-name", Input).value = "Analysis"
            await app._creator.submit()
            assert app._creator.pending is None
            assert not app.query_one("#session-create", Button).disabled
            await app._creator.submit()
            assert len(client.calls) == 2 and client.calls[0][2] != client.calls[1][2]
    asyncio.run(scenario())


@pytest.mark.parametrize("code,status", [("session_capacity", 409), ("forbidden", 403)])
def test_tui_rejected_recovery_cannot_discard_an_earlier_unknown_request(code, status):
    async def scenario():
        client = Client()
        client.fail = HubError("lost acknowledgement", code="hub_unavailable")
        app = HubTUI(client)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.query_one("#session-name", Input).value = "Analysis"
            await app._creator.submit()
            key = app._creator.pending["key"]
            client.fail = HubError("recovery refused", code=code, status=status)
            await app._creator.submit()
            assert app._creator.pending["key"] == key
            await app._creator.submit()
            assert len(client.calls) == 3
            assert {call[2] for call in client.calls} == {key}
    asyncio.run(scenario())


@pytest.mark.parametrize("online,capacity", [(False, 8), (True, 0), (True, True), (True, "8")])
def test_tui_does_not_offer_creation_on_offline_or_legacy_instances(online, capacity):
    async def scenario():
        client = Client()
        client.list_instances = lambda: [{"instance_id": "alpha", "online": online,
                                          "session_capacity": capacity}]
        app = HubTUI(client)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            assert app.query_one("#session-create", Button).disabled
    asyncio.run(scenario())
