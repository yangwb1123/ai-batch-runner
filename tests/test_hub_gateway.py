import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from pbatch.hub_client import HubError
from pbatch.hub_gateway import HubGateway, HubGatewayConfig
from pbatch.hub_gateway_protocol import project_event_payload, split_utf8
from pbatch.tui_protocol import RuntimeEvent


class _FakeSession:
    counter = 0

    def __init__(self, *, block=False):
        type(self).counter += 1
        self.local_id = f"local-{type(self).counter}"
        self.state = {"session_id": self.local_id}
        self.listener = None
        self.calls = 0
        self.block = block
        self.release = threading.Event()
        self.started = threading.Event()
        self.aborted = False

    def subscribe(self, listener):
        self.listener = listener

    def _event(self, kind, runtime_turn="runtime-turn"):
        event = RuntimeEvent(
            event_id="evt_" + uuid.uuid4().hex, seq=self.calls,
            occurred_at="2026-01-01T00:00:00Z", workspace_id="proj",
            session_id=self.local_id, run_id="runtime-run", turn_id=runtime_turn,
            actor_id="agent", kind=kind,
            payload={"text": "answer"} if kind == "message.delta" else {})
        self.listener(event)
        return event

    def prompt(self, _prompt):
        self.calls += 1
        self._event("turn.started")
        self.started.set()
        self._event("message.delta")
        if self.block:
            self.release.wait(timeout=2)
        kind = "turn.interrupted" if self.aborted else "turn.completed"
        terminal = self._event(kind)
        return SimpleNamespace(terminal_event=terminal)

    def abort(self):
        self.aborted = True
        self.release.set()

    def close(self):
        self.release.set()


class _FakeClient:
    def __init__(self, generation=1):
        self.next_generation = generation
        self.turn_state = "running"
        self.commands = []
        self.events = []
        self.registered = []
        self.fail_running_once = False
        self.terminal_running_once = False

    def register_instance(self, **_values):
        return {"generation": self.next_generation}

    def heartbeat(self, **_values):
        return {"ok": True}

    def register_session(self, **values):
        self.registered.append(values)
        return {"session_id": "hub-" + values["local_session_id"]}

    def get_commands(self, **_values):
        return list(self.commands)

    def get_session_requests(self, **_values):
        return []

    def get_session_close_requests(self, **_values):
        return []

    def send_event(self, event, **_values):
        self.events.append(event)
        if event["kind"] == "turn.running" and self.terminal_running_once:
            self.terminal_running_once = False
            raise HubError("turn already terminal", code="turn_terminal", status=409)
        if event["kind"] == "turn.running" and self.fail_running_once:
            self.fail_running_once = False
            raise HubError("temporary network failure", code="hub_unavailable")
        return {"cursor": str(len(self.events)), "turn_state": self.turn_state}


def _options(directory, **overrides):
    values = dict(hub_url="http://127.0.0.1:8788", cwd=str(directory),
                  state_dir=str(directory / "state"), name="worker",
                  project_id="project", heartbeat_interval=30,
                  poll_interval=0.1)
    values.update(overrides)
    return HubGatewayConfig(**values)


def _command(session, turn="hub-turn"):
    return {"command_id": "cmd-1", "turn_id": turn,
            "session_id": "hub-" + session.local_id,
            "local_session_id": session.local_id, "prompt": "do work",
            "state": "queued", "cancel_requested": False}


def _wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_gateway_maps_runtime_turn_to_hub_and_receipts_prevent_replay(tmp_path):
    client = _FakeClient()
    session = _FakeSession()
    gateway = HubGateway(_options(tmp_path), client=client,
                         session_factory=lambda _options: session)
    gateway.start()
    command = _command(session)
    client.commands = [command]
    try:
        gateway.run_once()
        _wait_for(lambda: session.calls == 1 and not gateway._turn_threads)
        gateway._dispatch_command(command)
        gateway._flush_outbox()
        assert session.calls == 1
        assert [item["kind"] for item in client.events] == [
            "turn.running", "message.delta", "turn.completed"]
        assert all(item["turn_id"] == "hub-turn" for item in client.events)
        assert client.events[0]["event_id"]
        assert "do work" not in repr(gateway._store.pending_events())
    finally:
        gateway.close()


def test_gateway_cancel_aborts_the_managed_session(tmp_path):
    client = _FakeClient()
    session = _FakeSession(block=True)
    gateway = HubGateway(_options(tmp_path), client=client,
                         session_factory=lambda _options: session)
    gateway.start()
    command = _command(session)
    client.commands = [command]
    try:
        gateway.run_once()
        assert session.started.wait(timeout=2)
        gateway._dispatch_command({**command, "cancel_requested": True})
        _wait_for(lambda: not gateway._turn_threads)
        gateway._flush_outbox()
        assert session.aborted
        assert client.events[-1]["kind"] == "turn.interrupted"
    finally:
        gateway.close()


def test_restart_marks_old_receipts_uncertain_and_sessions_read_only(tmp_path):
    state = tmp_path / "state"
    client = _FakeClient(generation=1)
    first = HubGateway(_options(tmp_path), client=client,
                       session_factory=lambda _options: _FakeSession())
    first.start()
    old_session = first._store.sessions()[0]
    first._store.accept_turn(turn_id="old-turn",
                             local_session_id=old_session["local_session_id"],
                             prompt_digest="digest")
    first._store.set_turn("old-turn", "executing")
    first.close()

    second = HubGateway(_options(tmp_path), client=_FakeClient(generation=2),
                        session_factory=lambda _options: _FakeSession())
    try:
        old = second._store.session(old_session["local_session_id"])
        assert old["controllable"] == 0
        assert second._store.turn("old-turn")["state"] == "uncertain"
        second.start()
        assert len(second._store.sessions()) == 2
    finally:
        second.close()


def test_state_directory_rejects_a_second_gateway(tmp_path):
    first = HubGateway(_options(tmp_path), client=_FakeClient(),
                       session_factory=lambda _options: _FakeSession())
    with pytest.raises(RuntimeError, match="another Gateway"):
        HubGateway(_options(tmp_path), client=_FakeClient(),
                   session_factory=lambda _options: _FakeSession())
    first.close()


def test_fencing_aborts_sessions_and_stops_gateway(tmp_path):
    client = _FakeClient()
    session = _FakeSession(block=True)
    gateway = HubGateway(_options(tmp_path), client=client,
                         session_factory=lambda _options: session)
    gateway.start()
    session.started.set()
    gateway._fence_and_stop(HubError("fenced", code="stale_generation", status=409))
    try:
        assert gateway._stop.is_set()
        assert session.aborted
        with pytest.raises(RuntimeError, match="fencing generation"):
            gateway._raise_if_fatal()
    finally:
        gateway.close()


def test_gateway_retries_same_running_ack_before_starting_provider(tmp_path):
    client = _FakeClient()
    client.fail_running_once = True
    session = _FakeSession()
    gateway = HubGateway(_options(tmp_path), client=client,
                         session_factory=lambda _options: session)
    gateway.start()
    client.commands = [_command(session)]
    try:
        gateway.run_once()
        assert session.calls == 0
        first = client.events[0]["event_id"]
        gateway.run_once()
        _wait_for(lambda: session.calls == 1 and not gateway._turn_threads)
        running = [item for item in client.events if item["kind"] == "turn.running"]
        assert len(running) == 2
        assert {item["event_id"] for item in running} == {first}
    finally:
        gateway.close()


def test_gateway_does_not_start_when_running_ack_reports_cancel(tmp_path):
    client = _FakeClient()
    client.turn_state = "cancel_requested"
    session = _FakeSession()
    gateway = HubGateway(_options(tmp_path), client=client,
                         session_factory=lambda _options: session)
    gateway.start()
    client.commands = [_command(session)]
    try:
        gateway.run_once()
        assert session.calls == 0
        assert client.events[-1]["kind"] == "turn.interrupted"
        assert gateway._store.turn("hub-turn")["state"] == "interrupted"
    finally:
        gateway.close()


def test_terminal_race_obsoletes_unstarted_running_event(tmp_path):
    client = _FakeClient()
    client.terminal_running_once = True
    session = _FakeSession()
    gateway = HubGateway(_options(tmp_path), client=client,
                         session_factory=lambda _options: session)
    gateway.start()
    client.commands = [_command(session)]
    try:
        gateway.run_once()
        assert session.calls == 0
        assert gateway._store.turn("hub-turn")["state"] == "cancelled"
        assert gateway._store.pending_events() == []
        assert gateway._pending_starts == {}
        gateway.run_once()
        assert session.calls == 0
    finally:
        gateway.close()


def test_runtime_message_projection_is_utf8_chunked_and_bounded():
    text = "猫" * 16000
    event = RuntimeEvent(
        event_id="evt_projection", seq=1, occurred_at="now", workspace_id="p",
        session_id="s", run_id="r", turn_id="t", actor_id="a",
        kind="message.completed", payload={"text": text, "access_token": "hide"})
    chunks = project_event_payload(event)
    assert len(chunks) > 1
    assert all(len(item["text"].encode("utf-8")) <= 16 * 1024 for item in chunks)
    assert all(item["truncated"] is False for item in chunks)
    assert "access_token" not in repr(chunks)
    bounded, truncated = split_utf8("猫" * 100000, 16 * 1024, 256 * 1024)
    assert truncated and sum(len(item.encode("utf-8")) for item in bounded) <= 256 * 1024


def test_json_escaped_control_characters_fit_event_envelope():
    import json
    text = "\x00" * 8000
    event = RuntimeEvent(
        event_id="evt_escaped", seq=1, occurred_at="now", workspace_id="p",
        session_id="s", run_id="r", turn_id="t", actor_id="a",
        kind="message.completed", payload={"text": text})
    chunks = project_event_payload(event)
    assert "".join(item["text"] for item in chunks) == text
    assert all(len(json.dumps(item, ensure_ascii=False).encode()) < 30 * 1024 for item in chunks)
