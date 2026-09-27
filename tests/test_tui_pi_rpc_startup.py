"""Pi's session-name notification must keep the startup journal identity."""

from __future__ import annotations

import asyncio
import json
import queue

import pytest

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_adapter_pi_rpc import PiRpcAdapter
from pbatch.tui_controller import TUIEventController
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_projection_ui import TUIProjectionMixin


class NamingNotificationTransport:
    """Match Pi 0.87: notify before acknowledging set_session_name."""

    def __init__(self):
        self.incoming = queue.Queue()
        self.commands = []
        self.session_number = 1

    def send_line(self, line):
        command = json.loads(line)
        kind = command["type"]
        self.commands.append(kind)
        if kind == "new_session":
            self.session_number += 1
        if kind == "set_session_name":
            self.incoming.put(json.dumps({
                "type": "session_info_changed", "sessionName": command["name"]}) + "\n")
        state = {"sessionFile": f"/sessions/{self.session_number}.jsonl",
                 "isStreaming": False, "messageCount": 0, "pendingMessageCount": 0}
        self.incoming.put(json.dumps({
            "id": command["id"], "type": "response", "command": kind,
            "success": True, "data": state if kind == "get_state" else {}}) + "\n")

    def recv_line(self):
        return self.incoming.get()

    def close(self):
        self.incoming.put(None)


def _drain(adapter):
    stream, events = adapter.events(), []
    while True:
        try:
            event = stream.get(timeout=0)
        except queue.Empty:
            return events
        if event is None:
            return events
        events.append(event)


@pytest.mark.parametrize("requested", ["", "chosen-local-session"])
def test_startup_name_notification_uses_adopted_journal_identity(tmp_path, monkeypatch, requested):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    transport = NamingNotificationTransport()
    adapter = PiRpcAdapter(tmp_path, transport=transport)
    initial = adapter.session.session_id
    controller = None
    try:
        adapter.initialize({})
        session = adapter.start_session({"name": "startup", "session_id": requested,
                                         "workspace_id": str(tmp_path)})
        assert session.session_id == (requested or initial)
        events = _drain(adapter)
        assert [event.kind for event in events] == ["runtime.unknown_item", "session.created"]
        assert events[0].payload["provider_event"] == "session_info_changed"
        TUIProjectionMixin._validate_idle_sessions(events, {session.session_id})
        assert {event.workspace_id for event in events} == {str(tmp_path)}
        controller = TUIEventController(journal=RuntimeEventJournal(str(tmp_path), session.session_id))
        for event in events:
            controller.ingest(event)
        assert not controller.read_only_reason
    finally:
        adapter.close()
        if controller is not None:
            controller.close()


def test_named_pi_rpc_startup_mounts_with_a_durable_session(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    transport = NamingNotificationTransport()
    adapter = PiRpcAdapter(tmp_path, transport=transport)

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path), agent="pi", runtime="pi-rpc",
                                   session_name="startup"),
                        pi_rpc_factory=lambda *_args, **_kwargs: adapter)
        async with app.run_test(size=(100, 30)):
            assert app._adapter is adapter
            assert app._last_status != "Backend unavailable"
            assert app._journal_session_id == adapter.session.session_id
            assert not app._runtime_blocked_reason
            events = RuntimeEventJournal(str(tmp_path), app._journal_session_id).recover().events
            assert [event.kind for event in events] == ["runtime.unknown_item", "session.created"]

    asyncio.run(scenario())
    assert transport.commands == ["get_state", "set_session_name", "get_state"]


def test_next_session_still_gets_a_fresh_local_identity(tmp_path):
    adapter = PiRpcAdapter(tmp_path, transport=NamingNotificationTransport())
    try:
        adapter.initialize({})
        first = adapter.start_session({"name": "first"})
        _drain(adapter)
        second = adapter.start_session({"name": "second"})
        assert second.session_id != first.session_id
        assert second.provider_session_id != first.provider_session_id
    finally:
        adapter.close()
