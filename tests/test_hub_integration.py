"""Real HTTP, SQLite, Gateway and AgentSession integration without a paid provider."""

from __future__ import annotations

import hashlib
import queue
import threading
import time
from dataclasses import replace

import pytest

from pbatch.agent_session import AgentSession
from pbatch.hub_auth import GATEWAY_SCOPE, USER_SCOPES, HubAuth
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_gateway import HubGateway, HubGatewayConfig
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore
from pbatch.tui_protocol import RuntimeCapabilities, RuntimeEvent
from pbatch.tui_runtime import RuntimeInfo, RuntimeSession, TurnHandle


class _Runtime:
    """Only the provider boundary is deterministic; orchestration stays real."""

    def __init__(self, hold=False):
        self.queue = queue.Queue()
        self.prompts = []
        self.hold = hold
        self.seq = 0
        self.handle = None

    def initialize(self, request):
        return self.runtime_info()

    def runtime_info(self):
        return RuntimeInfo("integration", 1, RuntimeCapabilities(
            runtime="integration", capabilities={"interrupt": True}))

    def start_session(self, request):
        self.session = RuntimeSession(request.session_id, "provider", request.workspace_id,
                                      "2026-09-12T00:00:00Z", "ACTIVE")
        return self.session

    def start_turn(self, request):
        self.prompts.append(request.prompt)
        index = len(self.prompts)
        self.handle = TurnHandle(self.session.session_id, f"run-{index}", f"turn-{index}",
                                 "agent", "2026-09-12T00:00:00Z")
        self.emit("turn.started")
        if not self.hold:
            self.emit("message.completed", {"text": f"Processed: {request.prompt}"})
            self.emit("turn.completed", {"usage": {"output_tokens": 10}})
        return self.handle

    def emit(self, kind, payload=None):
        self.seq += 1
        handle = self.handle
        self.queue.put(RuntimeEvent(
            event_id=f"evt_{self.seq}", seq=self.seq, occurred_at="2026-09-12T00:00:00Z",
            workspace_id=self.session.workspace_id, session_id=handle.session_id,
            run_id=handle.run_id, turn_id=handle.turn_id, actor_id="agent", kind=kind,
            payload=payload or {}))
        if kind in {"turn.completed", "turn.interrupted"}:
            handle._done.set()

    def interrupt_turn(self, request):
        self.emit("turn.interrupted")

    def events(self):
        while True:
            event = self.queue.get()
            if event is None:
                return
            yield event

    def close(self):
        self.queue.put(None)


def _grant(token, kind, instances, scopes):
    return {"tenant_id": "team", "subject": token, "client_id": token, "kind": kind,
            "instances": instances, "scopes": list(scopes),
            "token_sha256": hashlib.sha256(token.encode()).hexdigest()}


@pytest.fixture
def fleet(tmp_path):
    clock = [1000.0]
    grants = [_grant("user", "user", ["alpha", "beta"], USER_SCOPES)]
    grants += [_grant(name, "gateway", [name], [GATEWAY_SCOPE]) for name in ("alpha", "beta")]
    store = HubStore(tmp_path / "hub.sqlite3", clock=lambda: clock[0])
    server = HubHTTPServer(("127.0.0.1", 0), store,
                           HubAuth({"mode": "development", "grants": grants}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    user = HubClient(origin, "user")
    gateways = []

    def create(name, *, client_type=HubClient, hold=False):
        runtime = _Runtime(hold)
        options = HubGatewayConfig(origin, cwd=str(tmp_path), instance_id=name,
                                   state_dir=str(tmp_path / name), poll_interval=0.1)
        gateway = HubGateway(options, client=client_type(origin, name), session_factory=(
            lambda opts: AgentSession(runtime, replace(opts, session_id="same-local-id"))))
        gateways.append(gateway)
        gateway.start()
        session = user.list_sessions(name)[0]["session_id"]
        return gateway, runtime, session

    yield user, create, clock
    for gateway in gateways:
        gateway.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    store.close()


def _until(gateways, predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for gateway in gateways:
            gateway.run_once()
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "integration state did not converge"


def test_two_instances_route_once_and_publish_real_session_events(fleet):
    user, create, _clock = fleet
    alpha, aruntime, asession = create("alpha")
    beta, bruntime, bsession = create("beta")
    assert asession != bsession
    first = user.send_prompt(asession, "alpha task", "alpha-request")
    assert user.send_prompt(asession, "alpha task", "alpha-request") == first
    second = user.send_prompt(bsession, "beta task", "beta-request")
    turns = [first["turn_id"], second["turn_id"]]
    _until([alpha, beta], lambda: all(user.get_turn(t)["state"] == "completed" for t in turns))
    assert aruntime.prompts == ["alpha task"]
    assert bruntime.prompts == ["beta task"]
    events = user.session_events(asession)
    assert [e["kind"] for e in events] == ["turn.queued", "turn.running",
                                           "message.completed", "turn.completed"]
    assert events[0]["payload"]["text"] == "alpha task"
    assert events[2]["payload"]["text"] == "Processed: alpha task"
    assert user.session_events(asession, after=str(events[-2]["cursor"])) == events[-1:]
    assert user.get_session(asession)["active_turn_id"] == ""


def test_live_cancel_stops_runtime_and_next_turn_can_run(fleet):
    user, create, _clock = fleet
    gateway, runtime, session = create("alpha", hold=True)
    turn = user.send_prompt(session, "long task", "first")["turn_id"]
    _until([gateway], lambda: bool(runtime.prompts))
    assert user.cancel_turn(turn)["state"] == "cancel_requested"
    _until([gateway], lambda: user.get_turn(turn)["state"] == "interrupted")
    runtime.hold = False
    next_turn = user.send_prompt(session, "next task", "second")["turn_id"]
    _until([gateway], lambda: user.get_turn(next_turn)["state"] == "completed")
    assert runtime.prompts == ["long task", "next task"]


def test_lost_start_ack_then_cancel_does_not_execute(fleet):
    user, create, _clock = fleet

    class LostAck(HubClient):
        dropped = False
        skip_poll = False

        def send_event(self, event, **kwargs):
            response = super().send_event(event, **kwargs)
            if event["kind"] == "turn.running" and not self.dropped:
                self.dropped = self.skip_poll = True
                user.cancel_turn(event["turn_id"])
                raise HubError("simulated lost acknowledgement", code="hub_unavailable")
            return response

        def get_commands(self, **kwargs):
            if self.skip_poll:
                self.skip_poll = False
                return []
            return super().get_commands(**kwargs)

    gateway, runtime, session = create("alpha", client_type=LostAck)
    turn = user.send_prompt(session, "never execute", "cancel-race")["turn_id"]
    _until([gateway], lambda: user.get_turn(turn)["state"] == "interrupted")
    assert runtime.prompts == []


def test_cancel_between_command_poll_and_start_does_not_block_later_turn(fleet):
    user, create, _clock = fleet

    class CancelAfterPoll(HubClient):
        cancelled = False

        def get_commands(self, **kwargs):
            commands = super().get_commands(**kwargs)
            if commands and not self.cancelled:
                self.cancelled = True
                user.cancel_turn(commands[0]["turn_id"])
            return commands

    gateway, runtime, session = create("alpha", client_type=CancelAfterPoll)
    turn = user.send_prompt(session, "never execute", "first")["turn_id"]
    _until([gateway], lambda: user.get_turn(turn)["state"] == "cancelled")
    next_turn = user.send_prompt(session, "next task", "second")["turn_id"]
    _until([gateway], lambda: user.get_turn(next_turn)["state"] == "completed")
    assert runtime.prompts == ["next task"]
