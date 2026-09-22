"""Durable request browsing never replays work or steals a newer selection."""

import asyncio
import threading

import pytest
from textual.widgets import Button, Select, Static

from pbatch.hub_client import HubError
from pbatch.hub_session_operations_tui import SessionOperations
from pbatch.hub_tui import HubTUI
from test_hub_session_operations_client import close_record, record


class Client:
    def __init__(self):
        self.calls = []
        self.auth = 1
        self.failure = None
        self.gate = None
        self.block = ""
        self.entered = threading.Event()
        self.first = [record(status="queued", session_id=""), close_record()]
        self.older = [record(request_id="older", name="Earlier", created_at=1)]

    def _auth_context(self):
        return self.auth

    def _wait(self, kind):
        if self.block == kind and self.gate:
            self.entered.set()
            assert self.gate.wait(5)

    def list_instances(self):
        return [{"instance_id": "alpha", "online": False}, {"instance_id": "beta", "online": True}]

    def list_sessions(self, instance_id):
        return [dict(session_id="current", instance_id=instance_id, name="Current", status="idle",
                     controllable=True)]

    def session_events(self, session_id, **_kwargs):
        return [{"cursor": 1, "kind": "message.completed", "payload": {"text": "History " + session_id}}]

    def list_session_operations(self, instance_id="", *, before="", limit=20):
        self.calls.append(("list", instance_id, before, limit))
        self._wait("list")
        if self.failure:
            raise self.failure
        return {"data": [dict(row) for row in (self.older if before else self.first)],
                "next_cursor": "" if before else "older_cursor"}

    def session_request(self, request_id):
        self.calls.append(("detail", request_id))
        self._wait("detail")
        return {key: value for key, value in record(request_id=request_id).items() if key != "operation"}

    def session_close_request(self, request_id):
        self.calls.append(("close-detail", request_id))
        return {key: value for key, value in close_record(request_id=request_id).items()
                if key not in {"operation", "name"}}

    def get_session(self, session_id):
        self.calls.append(("session", session_id))
        self._wait("session")
        return dict(session_id=session_id, instance_id="alpha", name="Historical", status="closed",
                    online=False, controllable=False, frozen=True, active_turn_id="")

    def create_session(self, *_a, **_k):
        pytest.fail("history must not replay a creation")

    def close_session(self, *_a, **_k):
        pytest.fail("history must not replay a close")


async def show(app, pilot):
    await pilot.pause()
    await pilot.click("#session-requests")
    await pilot.pause()
    assert isinstance(app.screen, SessionOperations)
    return app.screen


def test_offline_instance_history_paginates_one_page_and_preserves_unknown_workflow_keys():
    async def scenario():
        client, app = Client(), None
        app = HubTUI(client)
        async with app.run_test(size=(110, 42)) as pilot:
            await pilot.pause()
            create = {"key": "unknown-create", "instance_id": "alpha", "name": "Review", "record": {}}
            close = {"key": "unknown-close", "session_id": "current", "record": {}}
            app._creator.pending, app._closer.pending = create, close
            screen = await show(app, pilot)
            assert len(screen.rows) == 2 and app._selected_session == "current"
            await screen.load_page(screen.next_cursor)
            assert list(screen.rows) == ["create:older"] and screen.next_cursor == ""
            assert screen.query_one("#operations-older", Button).disabled
            await screen.load_page()
            assert len(screen.rows) == 2 and "create:older" not in screen.rows
            assert app._creator.pending is create and app._closer.pending is close
            assert create["key"] == "unknown-create" and close["key"] == "unknown-close"
            assert all(call[0] == "list" for call in client.calls)
    asyncio.run(scenario())


def test_selected_detail_uses_get_and_only_explicit_open_selects_read_only_history():
    async def scenario():
        app = HubTUI(Client())
        async with app.run_test(size=(110, 42)) as pilot:
            screen = await show(app, pilot)
            assert screen.query_one("#operations-open", Button).disabled
            await screen.refresh_request()
            assert not screen.query_one("#operations-open", Button).disabled
            assert app._selected_session == "current"
            assert screen.rows["create:create-1"]["status"] == "created"
            await screen.open_session()
            await pilot.pause()
            assert app._selected_session == "session-1" and app._selection_generation > 0
            assert app.query_one("#send", Button).disabled
            assert "History session-1" in app.query_one("#events", Static).render().plain
            assert ("detail", "create-1") in app.client.calls
    asyncio.run(scenario())


def test_close_row_refresh_never_uses_creation_detail_or_closes_session():
    async def scenario():
        app = HubTUI(Client())
        async with app.run_test(size=(110, 42)) as pilot:
            screen = await show(app, pilot)
            screen.query_one("#operations-select", Select).value = "close:close-1"
            await pilot.pause()
            await screen.refresh_request()
            assert ("close-detail", "close-1") in app.client.calls
            assert not any(call[0] == "detail" for call in app.client.calls)
            assert app._selected_session == "current"
    asyncio.run(scenario())


@pytest.mark.parametrize("status,hint", [(401, "fleet auth login"), (403, "Access denied"),
                                      (404, "does not support"), (405, "does not support"),
                                      (501, "does not support"), (503, "refresh to retry")])
def test_failed_list_shows_safe_retry_or_old_hub_message(status, hint):
    async def scenario():
        client = Client()
        client.failure = HubError("PRIVATE response", status=status)
        app = HubTUI(client)
        async with app.run_test(size=(110, 42)) as pilot:
            screen = await show(app, pilot)
            text = screen.query_one("#operations-status", Static).render().plain
            assert hint in text and "PRIVATE" not in text
            assert not screen.query_one("#operations-latest", Button).disabled
            client.failure = None
            await screen.load_page()
            assert len(screen.rows) == 2
    asyncio.run(scenario())


def test_empty_instance_still_exposes_requests_and_empty_history_state():
    async def scenario():
        client = Client()
        client.list_sessions = lambda _instance: []
        client.first = []
        client.list_session_operations = lambda *_a, **_k: {"data": [], "next_cursor": ""}
        app = HubTUI(client)
        async with app.run_test(size=(80, 30)) as pilot:
            screen = await show(app, pilot)
            assert "No accepted" in screen.query_one("#operations-detail", Static).render().plain
            assert screen.query_one("#operations-open", Button).disabled
            assert screen.query_one("#operations-older", Button).disabled
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["dismiss", "instance", "selection", "auth"])
def test_late_explicit_open_cannot_steal_changed_context(change):
    async def scenario():
        client = Client()
        client.first = [record()]
        app = HubTUI(client)
        async with app.run_test(size=(110, 42)) as pilot:
            screen = await show(app, pilot)
            client.block, client.gate = "session", threading.Event()
            task = asyncio.create_task(screen.open_session())
            try:
                assert await asyncio.to_thread(client.entered.wait, 2)
                if change == "dismiss":
                    screen.key_escape()
                elif change == "instance":
                    app._selected_instance = "beta"
                    app._selection_generation += 1
                elif change == "selection":
                    app._selected_session = "new-selection"
                    app._selection_generation += 1
                else:
                    client.auth += 1
                client.gate.set()
                await task
                assert app._selected_session == ("new-selection" if change == "selection" else "current")
                assert "session-1" not in app._sessions
            finally:
                client.gate.set()
                await task
    asyncio.run(scenario())


def test_auth_change_during_list_discards_old_results_until_latest():
    async def scenario():
        client, app = Client(), None
        app = HubTUI(client)
        async with app.run_test(size=(110, 42)) as pilot:
            screen = await show(app, pilot)
            client.first = [record(request_id="new")]
            client.block, client.gate = "list", threading.Event()
            task = asyncio.create_task(screen.load_page())
            try:
                assert await asyncio.to_thread(client.entered.wait, 2)
                client.auth += 1
                client.gate.set()
                await task
                assert "create:new" not in screen.rows
                assert screen.rows == {} and screen.selected == "" and screen.next_cursor == ""
                assert screen.auth_context is None
                assert "Sign-in changed" in screen.query_one("#operations-status", Static).render().plain
                client.block = ""
                await screen.load_page()
                assert list(screen.rows) == ["create:new"]
            finally:
                client.gate.set()
                await task
    asyncio.run(scenario())


def test_account_change_prevents_using_an_old_page_cursor_and_clears_visible_records():
    async def scenario():
        client = Client()
        app = HubTUI(client)
        async with app.run_test(size=(110, 42)) as pilot:
            screen = await show(app, pilot)
            count = len(client.calls)
            client.auth += 1
            await screen.load_page(screen.next_cursor)
            assert len(client.calls) == count
            assert not screen.rows and not screen.next_cursor and not screen.selected
            assert screen.query_one("#operations-open", Button).disabled
            assert not screen.query_one("#operations-latest", Button).disabled
            await screen.load_page()
            assert len(screen.rows) == 2
    asyncio.run(scenario())


def test_late_detail_for_a_different_selected_request_is_discarded():
    async def scenario():
        client = Client()
        app = HubTUI(client)
        async with app.run_test(size=(110, 42)) as pilot:
            screen = await show(app, pilot)
            client.block, client.gate = "detail", threading.Event()
            task = asyncio.create_task(screen.refresh_request())
            try:
                assert await asyncio.to_thread(client.entered.wait, 2)
                screen.query_one("#operations-select", Select).value = "close:close-1"
                await pilot.pause()
                client.gate.set()
                await task
                assert screen.rows["create:create-1"]["status"] == "queued"
                assert screen.rows["close:close-1"]["status"] == "closed"
                assert app._selected_session == "current"
            finally:
                client.gate.set()
                await task
    asyncio.run(scenario())


def test_dismiss_during_list_keeps_host_selection_and_pending_identity():
    async def scenario():
        client = Client()
        app = HubTUI(client)
        async with app.run_test(size=(110, 42)) as pilot:
            screen = await show(app, pilot)
            client.block, client.gate = "list", threading.Event()
            task = asyncio.create_task(screen.load_page())
            try:
                assert await asyncio.to_thread(client.entered.wait, 2)
                screen.key_escape()
                client.gate.set()
                await task
                assert app._selected_session == "current" and app._creator.pending is None
            finally:
                client.gate.set()
                await task
    asyncio.run(scenario())
