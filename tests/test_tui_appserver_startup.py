"""Codex initialization notifications belong to the first adopted session."""

from __future__ import annotations

import asyncio
import json
import queue

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_adapter_appserver import CodexAppServerAdapter
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_projection_ui import TUIProjectionMixin


class StartupNotificationTransport:
    """Emit connection status before initialization completes, as Codex does."""

    def __init__(self):
        self.incoming = queue.Queue()
        self.methods = []
        self.thread_number = 0

    def send_line(self, line):
        command = json.loads(line)
        method = command["method"]
        self.methods.append(method)
        if method == "initialize":
            self._push({"method": "remoteControl/status/changed", "params": {"status": "disabled"}})
        if "id" not in command:
            return
        result = {}
        if method == "thread/start":
            self.thread_number += 1
            result = {"thread": {"id": f"thread-{self.thread_number}"}}
            self._push({"method": "thread/started", "params": result})
        self._push({"id": command["id"], "result": result})

    def _push(self, value):
        self.incoming.put(json.dumps(value) + "\n")

    def recv_line(self):
        return self.incoming.get()

    def close(self):
        self.incoming.put(None)


def _drain(adapter):
    events, stream = [], adapter.events()
    while True:
        try:
            event = stream.get(timeout=0)
        except queue.Empty:
            return events
        if event is None:
            return events
        events.append(event)


def test_initialize_notification_and_thread_share_local_identity(tmp_path):
    adapter = CodexAppServerAdapter(tmp_path, transport=StartupNotificationTransport())
    initial = adapter.session.session_id
    try:
        adapter.initialize({})
        session = adapter.start_session({})
        assert session.session_id == initial
        events = _drain(adapter)
        assert [event.kind for event in events] == ["runtime.unknown_item", "session.created"]
        TUIProjectionMixin._validate_idle_sessions(events, {session.session_id})
        assert {event.workspace_id for event in events} == {session.workspace_id}
        second = adapter.start_session({})
        assert second.session_id != initial
        assert second.provider_session_id != session.provider_session_id
    finally:
        adapter.close()


def test_appserver_mount_accepts_initialization_notifications(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    transport = StartupNotificationTransport()
    adapter = CodexAppServerAdapter(tmp_path, transport=transport)

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path), agent="codex", runtime="app-server"),
                        appserver_factory=lambda *_args, **_kwargs: adapter)
        async with app.run_test(size=(100, 30)):
            assert app._adapter is adapter
            assert app._last_status != "Backend unavailable"
            assert not app._runtime_blocked_reason
            assert app._journal_session_id == adapter.session.session_id
            events = RuntimeEventJournal(str(tmp_path), app._journal_session_id).recover().events
            assert [event.kind for event in events] == ["runtime.unknown_item", "session.created"]

    asyncio.run(scenario())
    assert transport.methods == ["initialize", "initialized", "thread/start"]
