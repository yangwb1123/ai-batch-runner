"""Read-only placement modal keeps one page and rejects stale task/identity results."""

import asyncio
import threading

import pytest
from textual.widgets import Button, Static

from pbatch.hub_client import HubError
from pbatch.hub_compute_placement_tui import ComputePlacement
from pbatch.hub_compute_tui import ComputeTUI
from test_hub_compute_tui import _Compute
from test_hub_placement_clients import device, observation


class Compute(_Compute):
    def __init__(self):
        super().__init__()
        self.token = "first"
        self.client._auth_context = lambda: ("hub", self.token)
        self.tasks_by_session["s1"] = [{"task_id": "task-1", "session_id": "s1", "state": "queued"}]
        self.calls = []
        self.response = observation()
        self.error = None
        self.blocked = False
        self.started = threading.Event()
        self.release = threading.Event()

    def placement(self, task_id, *, after="", limit=20):
        self.calls.append((task_id, after, limit))
        response, error = self.response, self.error
        if self.blocked:
            self.started.set()
            self.release.wait(3)
        if error:
            raise error
        return response


async def open_view(app, pilot):
    await pilot.pause(0.05)
    await app.on_button_pressed(Button.Pressed(app.query_one("#placement-task", Button)))
    await pilot.pause(0.1)
    assert isinstance(app.screen, ComputePlacement)
    return app.screen


def test_modal_retains_one_explicit_page_without_changing_submission_or_selection():
    async def scenario():
        client = Compute()
        app = ComputeTUI(client)
        app._pending[("s1", "fingerprint")] = "original-key"
        async with app.run_test(size=(80, 24)) as pilot:
            view = await open_view(app, pilot)
            assert view.next_cursor == "device-b" and len(client.calls) == 1
            assert "Fabric still decides" in view.query_one("#placement-detail", Static).render().plain
            client.response = observation(devices=[device(device_id="device-c")], next_cursor="")
            await view.load_page(view.next_cursor)
            assert client.calls[-1] == ("task-1", "device-b", 20)
            assert [row["device_id"] for row in view.result["devices"]] == ["device-c"]
            assert view.query_one("#placement-next", Button).disabled
            await view.load_page()
            assert client.calls[-1][1] == ""
            assert app._pending == {("s1", "fingerprint"): "original-key"}
            assert (app._selected_session, app._selected_task) == ("s1", "task-1")
            assert client.submissions == client.cancellations == []

    asyncio.run(scenario())


def test_changed_identity_cannot_use_previous_cursor_and_refresh_uses_current_identity():
    async def scenario():
        client, app = Compute(), None
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            view = await open_view(app, pilot)
            client.token = "second"
            await view.load_page(view.next_cursor)
            assert view.result is None and view.next_cursor == ""
            assert len(client.calls) == 1
            assert "Sign-in changed" in view.query_one("#placement-status", Static).render().plain
            await view.load_page()
            assert view.result == client.response and len(client.calls) == 2
            assert view.auth_context == ("hub", "second")

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403])
def test_permission_failure_clears_existing_rows_and_cursor(status):
    async def scenario():
        client = Compute()
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            view = await open_view(app, pilot)
            client.error = HubError("permission denied", status=status)
            await view.load_page()
            assert view.result is None and view.next_cursor == "" and view.auth_context is None
            assert view.query_one("#placement-detail", Static).render().plain == ""
            assert view.query_one("#placement-next", Button).disabled

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_late_response_cannot_restore_previous_identity(failure):
    async def scenario():
        client = Compute()
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            view = await open_view(app, pilot)
            client.blocked = True
            client.error = HubError("old permission denial", status=403) if failure else None
            pending = asyncio.create_task(view.load_page())
            assert await asyncio.to_thread(client.started.wait, 1)
            client.token = "second"
            client.release.set()
            await pending
            assert view.result is None and view.next_cursor == ""
            status = view.query_one("#placement-status", Static).render().plain
            assert "Sign-in changed" in status and "Access denied" not in status

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["task", "session", "away-back", "dismiss"])
def test_late_success_cannot_restore_previous_selection_or_closed_modal(action):
    async def scenario():
        client = Compute()
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            view = await open_view(app, pilot)
            client.blocked = True
            pending = asyncio.create_task(view.load_page())
            assert await asyncio.to_thread(client.started.wait, 1)
            if action == "dismiss":
                view.key_escape()
            elif action == "session":
                app._selection(session_id="s2")
            else:
                app._selection(task_id="other")
                if action == "away-back":
                    app._selection(task_id="task-1")
            client.release.set()
            await pending
            assert view.result is None and view.next_cursor == ""

    asyncio.run(scenario())


def test_nonqueued_view_and_old_hub_are_explicit_and_read_only():
    async def scenario():
        client = Compute()
        client.response = observation(state="running", devices=[], next_cursor="")
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            view = await open_view(app, pilot)
            assert "only while the task is queued" in view.query_one("#placement-detail", Static).render().plain
            assert view.query_one("#placement-next", Button).disabled
            client.error = HubError("old Hub", status=405)
            await view.load_page()
            assert "does not support" in view.query_one("#placement-status", Static).render().plain
            assert client.submissions == client.cancellations == []

    asyncio.run(scenario())


def test_busy_modal_does_not_fan_out_requests():
    async def scenario():
        client = Compute()
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            view = await open_view(app, pilot)
            client.blocked = True
            pending = asyncio.create_task(view.load_page())
            assert await asyncio.to_thread(client.started.wait, 1)
            await view.load_page(view.next_cursor)
            assert len(client.calls) == 2
            client.release.set()
            await pending

    asyncio.run(scenario())


def test_late_host_task_refresh_immediately_clears_visible_diagnostics():
    async def scenario():
        client = Compute()
        app = ComputeTUI(client)
        started, release = threading.Event(), threading.Event()
        async with app.run_test() as pilot:
            await pilot.pause(0.1)

            def delayed_tasks(_session):
                started.set()
                release.wait(3)
                return []

            client.tasks = delayed_tasks
            pending = asyncio.create_task(app._refresh_tasks())
            assert await asyncio.to_thread(started.wait, 1)
            view = await open_view(app, pilot)
            assert view.result is not None
            release.set()
            await pending
            assert view.result is None and view.next_cursor == ""
            assert view.query_one("#placement-detail", Static).render().plain == ""
            assert view.query_one("#placement-refresh", Button).disabled
            assert "selection changed" in view.query_one("#placement-status", Static).render().plain

    asyncio.run(scenario())


def test_unreadable_credentials_clear_page_without_another_network_read():
    async def scenario():
        client = Compute()
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            view = await open_view(app, pilot)

            def unavailable():
                raise HubError("credential unavailable", code="credential_unavailable", status=401)

            client.client._auth_context = unavailable
            await view.load_page()
            assert view.result is None and view.next_cursor == "" and len(client.calls) == 1
            assert "Sign in" in view.query_one("#placement-status", Static).render().plain

    asyncio.run(scenario())
