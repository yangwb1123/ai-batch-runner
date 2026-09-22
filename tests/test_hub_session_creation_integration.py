"""Real HTTP/SQLite/session creation with only model execution substituted."""

import threading

import pytest

from pbatch import hub_cli
from pbatch.agent_session import AgentSession
from pbatch.hub_auth import GATEWAY_SCOPE, USER_SCOPES, HubAuth
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_gateway import HubGateway, HubGatewayConfig
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore
from test_hub_integration import _Runtime, _grant, _until


@pytest.fixture
def fleet(tmp_path):
    clock = [1000.0]
    store = HubStore(tmp_path / "hub.sqlite3", clock=lambda: clock[0])
    grants = [_grant("user", "user", ["alpha", "beta"], USER_SCOPES)]
    grants += [_grant(name, "gateway", [name], [GATEWAY_SCOPE]) for name in ("alpha", "beta")]
    server = HubHTTPServer(("127.0.0.1", 0), store,
                           HubAuth({"mode": "development", "grants": grants}))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    gateways, runtimes = [], {"alpha": [], "beta": []}

    def create(name, *, sessions=0, client_type=HubClient, factory=None, maximum=3):
        def construct(options):
            runtime = _Runtime()
            runtimes[name].append(runtime)
            return AgentSession(runtime, options)

        options = HubGatewayConfig(origin, cwd=str(tmp_path), instance_id=name,
                                   state_dir=str(tmp_path / name), sessions=sessions,
                                   max_sessions=maximum, poll_interval=0.1, heartbeat_interval=1)
        gateway = HubGateway(options, client=client_type(origin, name),
                             session_factory=factory or construct)
        gateways.append(gateway)
        gateway.start()
        return gateway

    try:
        yield HubClient(origin, "user"), create, gateways, runtimes, store, clock
    finally:
        for gateway in gateways:
            gateway.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        store.close()


def test_two_empty_instances_create_fresh_sessions_and_route_prompts_once(fleet):
    user, create, gateways, runtimes, _store, _clock = fleet
    create("alpha")
    create("beta")
    assert user.list_sessions() == []
    assert all(row["session_capacity"] == 3 for row in user.list_instances())
    requests = [user.create_session(name, "Analysis", idempotency_key=name) for name in ("alpha", "beta")]
    _until(gateways, lambda: all(user.session_request(row["request_id"])["status"] == "created"
                                 for row in requests))
    sessions = [user.session_request(row["request_id"])["session_id"] for row in requests]
    assert len(set(sessions)) == 2
    for name, request, session_id in zip(("alpha", "beta"), requests, sessions):
        assert user.create_session(name, "Analysis", idempotency_key=name)["request_id"] == request["request_id"]
        assert len(runtimes[name]) == 1
        session = user.get_session(session_id)
        assert session["instance_id"] == name and session["controllable"]
        turn = user.send_prompt(session_id, name + " work", name + "-turn")
        assert user.send_prompt(session_id, name + " work", name + "-turn")["turn_id"] == turn["turn_id"]
        _until(gateways, lambda: user.get_turn(turn["turn_id"])["state"] == "completed")
        assert runtimes[name][0].prompts == [name + " work"]


def test_cli_unknown_creation_response_retries_the_same_durable_request(fleet, monkeypatch, capsys):
    user, create, gateways, runtimes, _store, _clock = fleet
    create("alpha")

    class LostAck(HubClient):
        dropped = False
        request_id = ""

        def create_session(self, *args, **kwargs):
            result = super().create_session(*args, **kwargs)
            self.request_id = result["request_id"]
            if not self.dropped:
                self.dropped = True
                raise HubError("PRIVATE unexpected body", code="hub_unavailable")
            return result

    client = LostAck(user.base_url, "user")
    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *_a, **_k: client)
    args = ["session-create", "alpha", "--name", "Analysis", "--idempotency-key", "cli-key", "--json"]
    assert hub_cli.hub_main(args) == 2
    output = capsys.readouterr()
    assert "cli-key" in output.err and "PRIVATE" not in output.err
    first = client.request_id
    assert hub_cli.hub_main(args) == 0
    assert client.request_id == first
    _until(gateways, lambda: user.session_request(first)["status"] == "created")
    assert len(runtimes["alpha"]) == 1
    assert hub_cli.hub_main(["session-request", first, "--json"]) == 0


def test_gateway_lost_creation_ack_reuses_the_object_and_preserves_queued_prompt(fleet):
    user, create, gateways, runtimes, _store, _clock = fleet

    class LostReport(HubClient):
        reports = 0

        def report_session_request(self, **body):
            result = super().report_session_request(**body)
            self.reports += 1
            if self.reports == 1:
                raise HubError("lost creation acknowledgement", code="hub_unavailable")
            return result

    gateway = create("alpha", client_type=LostReport)
    request = user.create_session("alpha", "Analysis", idempotency_key="create")
    _until(gateways, lambda: user.session_request(request["request_id"])["status"] == "created")
    session_id = user.session_request(request["request_id"])["session_id"]
    turn = user.send_prompt(session_id, "process once", "turn-after-creation")
    _until(gateways, lambda: user.get_turn(turn["turn_id"])["state"] == "completed")
    assert gateway.client.reports >= 2
    assert len(runtimes["alpha"]) == 1
    assert runtimes["alpha"][0].prompts == ["process once"]


def test_slow_factory_does_not_block_heartbeat_or_cancel_existing_session(fleet):
    user, create, gateways, _runtimes, store, clock = fleet
    entered, release = threading.Event(), threading.Event()
    live = _Runtime(hold=True)
    calls = []

    def factory(options):
        calls.append(options.session_id)
        if len(calls) == 1:
            return AgentSession(live, options)
        entered.set()
        assert release.wait(10)
        return AgentSession(_Runtime(), options)

    gateway = create("alpha", sessions=1, factory=factory)
    old_session = user.list_sessions("alpha")[0]["session_id"]
    request = user.create_session("alpha", "Background", idempotency_key="slow-create")
    try:
        _until(gateways, entered.is_set)
        clock[0] += 10
        gateway._last_heartbeat = 0
        turn = user.send_prompt(old_session, "running task", "old-turn")
        _until(gateways, lambda: bool(live.prompts))
        assert store.instances("team")[0]["last_seen"] == clock[0]
        user.cancel_turn(turn["turn_id"])
        _until(gateways, lambda: user.get_turn(turn["turn_id"])["state"] == "interrupted")
        assert user.session_request(request["request_id"])["status"] == "queued"
    finally:
        release.set()
    _until(gateways, lambda: user.session_request(request["request_id"])["status"] == "created")


def test_generation_takeover_loses_queued_creation_without_reconstructing_it(fleet):
    user, create, gateways, runtimes, _store, clock = fleet
    old = create("alpha")
    old.close()
    request = user.create_session("alpha", "Never started", idempotency_key="old-request")
    clock[0] += 61
    replacement = create("alpha")
    result = user.session_request(request["request_id"])
    assert result["status"] == "lost" and result["error_code"] == "owner_replaced"
    assert user.create_session("alpha", "Never started", idempotency_key="old-request") == result
    assert runtimes["alpha"] == []
    fresh = user.create_session("alpha", "Fresh", idempotency_key="new-request")
    _until([replacement], lambda: user.session_request(fresh["request_id"])["status"] == "created")
    assert len(runtimes["alpha"]) == 1
