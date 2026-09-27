"""An idle provider disconnect must remain recoverable through /new."""

from __future__ import annotations

import asyncio
import json
import queue
import time

import pytest

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_adapter_appserver import CodexAppServerAdapter
from pbatch.tui_adapter_pi_rpc import PiRpcAdapter
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_protocol import TurnState


class _Transport:
    def __init__(self, runtime, number):
        self.runtime, self.number = runtime, number
        self.incoming = queue.Queue()
        self.commands = []
        self.closed = False
        self.turns = 0

    def send_line(self, line):
        command = json.loads(line)
        self.commands.append(command)
        if self.runtime == "pi-rpc":
            self._pi_response(command)
        else:
            self._codex_response(command)

    def _pi_response(self, command):
        kind = command["type"]
        if kind == "prompt":
            self.turns += 1
        data = {"sessionFile": f"/sessions/{self.number}.jsonl",
                "isStreaming": False, "pendingMessageCount": 0}
        self.push({"id": command["id"], "type": "response", "command": kind,
                   "success": True, "data": data if kind == "get_state" else {}})

    def _codex_response(self, command):
        if "id" not in command:
            return
        kind = command["method"]
        result = {}
        if kind == "initialize":
            result = {"protocolVersion": 1, "schemaVersion": "0.149.0",
                      "serverInfo": {"name": "codex", "version": "0.149.0"}}
        elif kind == "thread/start":
            result = {"thread": {"id": f"thread-{self.number}"}}
        elif kind == "turn/start":
            self.turns += 1
            result = {"turn": {"id": f"turn-{self.turns}"}}
        self.push({"id": command["id"], "result": result})

    def begin(self):
        if self.runtime == "pi-rpc":
            self.push({"type": "agent_start"})
        else:
            self.push({"method": "turn/started", "params": {
                "threadId": f"thread-{self.number}", "turn": {
                    "id": f"turn-{self.turns}", "status": "inProgress"}}})

    def complete(self):
        self.begin()
        if self.runtime == "pi-rpc":
            self.push({"type": "message_end", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "done"}],
                "stopReason": "stop"}})
            self.push({"type": "agent_settled"})
        else:
            self.push({"method": "turn/completed", "params": {
                "threadId": f"thread-{self.number}", "turn": {
                    "id": f"turn-{self.turns}", "status": "completed"}}})

    def push(self, value):
        self.incoming.put(json.dumps(value) + "\n")

    def recv_line(self):
        return self.incoming.get()

    def close(self):
        self.closed = True
        self.incoming.put(None)


def _app(tmp_path, runtime, monkeypatch, *, retry_failure=False):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    adapters, transports = [], []
    attempts = []

    def factory(*args, **kwargs):
        attempts.append(None)
        if retry_failure and len(attempts) == 2:
            raise RuntimeError("provider is still restarting")
        transport = _Transport(runtime, len(transports) + 1)
        kind = PiRpcAdapter if runtime == "pi-rpc" else CodexAppServerAdapter
        timeouts = {"timeout": 1} if runtime == "pi-rpc" else {"request_timeout": 1}
        adapter = kind(tmp_path, transport=transport, **timeouts)
        transports.append(transport)
        adapters.append(adapter)
        return adapter

    app = PiBatchTUI(TuiOptions(cwd=str(tmp_path), agent="pi", runtime=runtime,
                               timeout=3, memory_mode="off"),
                    pi_rpc_factory=factory, appserver_factory=factory)
    return app, adapters, transports, attempts


async def _until(predicate, pilot):
    deadline = time.monotonic() + 4
    while not predicate() and time.monotonic() < deadline:
        await pilot.pause(0.01)
    assert predicate()


async def _success(app, adapter, transport, pilot):
    assert app._adapter is adapter
    assert app._start_turn("explicit user prompt")
    await _until(lambda: transport.turns > 0 and app._turn_handle is not None, pilot)
    transport.complete()
    await _until(lambda: not app._busy, pilot)
    assert app._runtime_view.turn_state == TurnState.SUCCEEDED


@pytest.mark.parametrize("runtime", ["pi-rpc", "app-server"])
@pytest.mark.parametrize("after_success", [False, True])
@pytest.mark.parametrize("fault", [None, "{malformed JSON\n"])
def test_new_reconnects_after_idle_disconnect(tmp_path, monkeypatch, runtime,
                                            after_success, fault):
    async def scenario():
        app, adapters, transports, attempts = _app(tmp_path, runtime, monkeypatch)
        async with app.run_test() as pilot:
            first, source = adapters[0], transports[0]
            if after_success:
                await _success(app, first, source, pilot)
            old_session = app._journal_session_id
            source.incoming.put(fault)
            await _until(lambda: first._stream_ended, pilot)
            assert app._runtime_operation is None
            app._slash_new(())
            assert len(attempts) == 2, "reconnect must construct a fresh adapter"
            assert app._adapter is adapters[1]
            assert source.closed
            assert not app._runtime_blocked_reason
            assert transports[1].turns == 0, "reconnecting must not replay a prompt"
            assert app._journal_session_id != old_session
            old_events = RuntimeEventJournal(str(tmp_path), old_session).recover().events
            assert any(event.kind == "runtime.error" for event in old_events)
            await _success(app, adapters[1], transports[1], pilot)
    asyncio.run(scenario())


@pytest.mark.parametrize("runtime", ["pi-rpc", "app-server"])
def test_new_can_retry_when_provider_reconnect_itself_fails(tmp_path, monkeypatch, runtime):
    async def scenario():
        app, adapters, transports, attempts = _app(
            tmp_path, runtime, monkeypatch, retry_failure=True)
        async with app.run_test() as pilot:
            transports[0].incoming.put(None)
            await _until(lambda: adapters[0]._stream_ended, pilot)
            app._slash_new(())
            assert app._runtime_blocked_reason
            assert len(attempts) == 2
            assert not app._start_turn("must not fall back to another backend")
            app._slash_new(())
            assert len(attempts) == 3
            assert not app._runtime_blocked_reason
            await _success(app, adapters[1], transports[1], pilot)
    asyncio.run(scenario())


@pytest.mark.parametrize("runtime", ["pi-rpc", "app-server"])
@pytest.mark.parametrize("cancel", [False, True])
def test_running_disconnect_or_cancel_stays_unknown_then_recovers(
        tmp_path, monkeypatch, runtime, cancel):
    async def scenario():
        app, adapters, transports, _ = _app(tmp_path, runtime, monkeypatch)
        async with app.run_test() as pilot:
            assert app._start_turn("unconfirmed operation")
            await _until(lambda: app._turn_handle is not None, pilot)
            transports[0].begin()
            await _until(lambda: app._runtime_view.turn_state == TurnState.RUNNING, pilot)
            old_session = app._journal_session_id
            if cancel:
                app.action_cancel()
            else:
                transports[0].incoming.put(None)
            await _until(lambda: not app._busy, pilot)
            assert app._runtime_view.turn_state == TurnState.UNKNOWN
            app._slash_new(())
            assert app._adapter is adapters[1]
            assert not app._runtime_blocked_reason
            assert transports[1].turns == 0
            old_events = RuntimeEventJournal(str(tmp_path), old_session).recover().events
            assert not any(event.kind == "turn.completed" for event in old_events)
            await _success(app, adapters[1], transports[1], pilot)
    asyncio.run(scenario())
