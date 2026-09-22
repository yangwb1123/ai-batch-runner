import asyncio
import pytest

from textual.widgets import Button, Static, TextArea

from pbatch.hub_tui import HubTUI, _ALL_INSTANCES, _MAX_PENDING_PROMPTS
from pbatch.hub_client import HubError


class _Client:
    def __init__(self):
        self.sent = []

    def list_instances(self):
        return [{"instance_id": "inst-1", "name": "worker", "status": "online"}]

    def list_sessions(self, _instance_id):
        return [{"session_id": "old", "name": "old", "status": "completed",
                 "controllable": False},
                {"session_id": "active", "name": "active", "status": "idle",
                 "controllable": True}]

    def session_events(self, session_id, *, after="", limit=100):
        del limit
        if session_id == "old" and not after:
            return [{"cursor": "c-1", "kind": "message.completed",
                     "payload": {"text": "historical output"}}]
        return []

    def send_prompt(self, session_id, prompt, idempotency_key=""):
        self.sent.append((session_id, prompt))
        return {"turn_id": "turn-1"}

    def get_session(self, _session_id):
        return {"active_turn_id": "turn-1"}

    def cancel_turn(self, turn_id):
        return {"turn_id": turn_id}


def test_tui_marks_history_read_only_and_sends_from_controllable_session():
    async def scenario():
        client = _Client()
        app = HubTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            app._selected_session = "old"
            app._update_controls()
            assert app.query_one("#send", Button).disabled
            assert app.query_one("#composer", TextArea).disabled
            await app._refresh_events()
            rendered = app.query_one("#events", Static).render().plain
            assert "historical output" in rendered

            app._selected_session = "active"
            app._update_controls()
            assert not app.query_one("#send", Button).disabled
            composer = app.query_one("#composer", TextArea)
            composer.text = "run this task"
            await app._send_prompt()
            assert client.sent == [("active", "run this task")]
            await pilot.pause()

    asyncio.run(scenario())


def test_tui_retains_request_identity_when_submission_ack_is_lost():
    class LostAck(_Client):
        def __init__(self):
            super().__init__()
            self.keys = []

        def send_prompt(self, session_id, prompt, idempotency_key=""):
            self.keys.append(idempotency_key)
            if len(self.keys) == 1:
                raise HubError("acknowledgement lost", code="hub_unavailable")
            return super().send_prompt(session_id, prompt, idempotency_key)

    async def scenario():
        client = LostAck()
        app = HubTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            app._selected_session = "active"
            app._update_controls()
            composer = app.query_one("#composer", TextArea)
            composer.text = "retry this task"
            await app._send_prompt()
            assert composer.text == "retry this task"
            assert "unconfirmed" in app.query_one("#status", Static).render().plain
            await app._send_prompt()
            assert len(client.keys) == 2 and client.keys[0] == client.keys[1]
            assert client.keys[0]
            assert composer.text == ""

    asyncio.run(scenario())


def test_tui_keeps_lost_ack_request_key_per_session():
    class SwitchingClient(_Client):
        def __init__(self):
            super().__init__()
            self.keys = []

        def list_sessions(self, _instance_id):
            return [{"session_id": session_id, "name": session_id,
                     "status": "idle", "controllable": True}
                    for session_id in ("session-a", "session-b")]

        def send_prompt(self, session_id, prompt, idempotency_key=""):
            self.keys.append((session_id, prompt, idempotency_key))
            if session_id == "session-a" and sum(
                    row[0] == session_id for row in self.keys) == 1:
                raise HubError("acknowledgement lost", code="hub_unavailable")
            return {"turn_id": f"turn-{len(self.keys)}"}

    async def scenario():
        client = SwitchingClient()
        app = HubTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            composer = app.query_one("#composer", TextArea)
            app._selected_session = "session-a"
            composer.text = "retry session A"
            await app._send_prompt()
            assert "unconfirmed" in app.query_one("#status", Static).render().plain

            app._selected_session = "session-b"
            composer.text = "session B prompt"
            await app._send_prompt()
            assert composer.text == ""

            app._selected_session = "session-a"
            composer.text = "retry session A"
            await app._send_prompt()

        a_keys = [key for session_id, _prompt, key in client.keys
                  if session_id == "session-a"]
        b_keys = [key for session_id, _prompt, key in client.keys
                  if session_id == "session-b"]
        assert len(a_keys) == 2 and a_keys[0] and a_keys[0] == a_keys[1]
        assert len(b_keys) == 1 and b_keys[0] and b_keys[0] != a_keys[0]

    asyncio.run(scenario())


def test_tui_can_aggregate_sessions_across_instances_without_enabling_instance_actions():
    class Aggregate(_Client):
        def __init__(self):
            super().__init__()
            self.filters = []

        def list_sessions(self, instance_id):
            self.filters.append(instance_id)
            if instance_id:
                return super().list_sessions(instance_id)
            return [
                {"session_id": "alpha-session", "instance_id": "alpha", "name": "Alpha",
                 "status": "idle", "controllable": True},
                {"session_id": "beta-session", "instance_id": "beta", "name": "Beta",
                 "status": "idle", "controllable": True},
            ]

    async def scenario():
        client = Aggregate()
        app = HubTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            app._selected_instance = _ALL_INSTANCES
            await app._refresh_sessions()
            assert client.filters[-1] == ""
            assert {row["instance_id"] for row in app._sessions.values()} == {"alpha", "beta"}
            assert app.query_one("#session-requests", Button).disabled
            app._selected_session = "beta-session"
            app._update_controls()
            app.query_one("#composer", TextArea).text = "run on beta"
            await app._send_prompt()
            assert client.sent[-1][0] == "beta-session"

    asyncio.run(scenario())


def test_tui_does_not_evict_unconfirmed_requests_when_at_capacity():
    async def scenario():
        client = _Client()
        app = HubTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            pending = {f"session-{index}": ("unconfirmed", f"key-{index}")
                       for index in range(_MAX_PENDING_PROMPTS)}
            app._pending_prompts = dict(pending)
            app._selected_session = "active"
            app.query_one("#composer", TextArea).text = "new task"
            await app._send_prompt()
            assert client.sent == []
            assert app._pending_prompts == pending
            assert "retry an existing session" in app.query_one("#status", Static).render().plain

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403])
def test_tui_auth_recovery_keeps_prompt_and_submission_key(status):
    class Rejected(_Client):
        def __init__(self):
            super().__init__()
            self.keys = []

        def send_prompt(self, session_id, prompt, idempotency_key=""):
            self.keys.append(idempotency_key)
            if len(self.keys) == 1:
                raise HubError("private upstream response", status=status)
            return super().send_prompt(session_id, prompt, idempotency_key)

    async def scenario():
        client = Rejected()
        app = HubTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            app._selected_session = "active"
            app._update_controls()
            composer = app.query_one("#composer", TextArea)
            composer.text = "preserve this prompt"
            await app._send_prompt()
            rendered = app.query_one("#status", Static).render().plain
            assert "private upstream response" not in rendered
            assert "unconfirmed" in rendered
            assert ("fleet auth login" in rendered) == (status == 401)
            assert ("permissions" in rendered) == (status == 403)
            assert composer.text == "preserve this prompt"
            await app._send_prompt()
            assert len(client.keys) == 2 and client.keys[0] == client.keys[1]
            assert composer.text == ""

    asyncio.run(scenario())
