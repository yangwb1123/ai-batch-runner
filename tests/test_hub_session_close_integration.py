"""Real HTTP/SQLite/AgentSession close lifecycle, with only provider execution replaced."""

import threading

import pytest

from pbatch import hub_cli
from pbatch.agent_session import AgentSession
from pbatch.hub_client import HubClient, HubError
from test_hub_integration import _Runtime, _until
from test_hub_session_creation_integration import fleet  # noqa: F401


class Runtime(_Runtime):
    def __init__(self, *, hold=False, fail=False, gate=None):
        super().__init__(hold)
        self.close_calls = 0
        self.fail = fail
        self.gate = gate
        self.entered = threading.Event()

    def close(self):
        self.close_calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.fail:
            raise RuntimeError("PRIVATE runtime could not close")
        super().close()


def created(user, gateways, instance, key):
    request = user.create_session(instance, "Analysis", idempotency_key=key)
    _until(gateways, lambda: user.session_request(request["request_id"])["status"] == "created")
    return user.session_request(request["request_id"])["session_id"]


def test_close_reclaims_capacity_without_losing_history_or_other_instances(fleet):
    user, create, gateways, runtimes, _store, _clock = fleet
    create("alpha", maximum=1)
    create("beta", maximum=1)
    first = created(user, gateways, "alpha", "first")
    other = created(user, gateways, "beta", "other")
    turn = user.send_prompt(first, "keep this history", "history")
    _until(gateways, lambda: user.get_turn(turn["turn_id"])["state"] == "completed")
    history = user.session_events(first)
    request = user.close_session(first, idempotency_key="close-first")
    _until(gateways, lambda: user.session_close_request(request["request_id"])["status"] == "closed")
    session = user.get_session(first)
    assert session["frozen"] and not session["controllable"] and session["status"] == "closed"
    assert user.session_events(first) == history
    with pytest.raises(HubError) as stopped:
        user.send_prompt(first, "must not run", "after-close")
    assert stopped.value.code == "session_unavailable"
    new_session = created(user, gateways, "alpha", "replacement")
    assert new_session != first and len(runtimes["alpha"]) == 2
    task = user.send_prompt(other, "other instance still works", "other-turn")
    _until(gateways, lambda: user.get_turn(task["turn_id"])["state"] == "completed")
    assert user.create_session("alpha", "Analysis", idempotency_key="first")["session_id"] == first
    assert user.close_session(first, idempotency_key="close-first")["request_id"] == request["request_id"]


def test_slow_close_keeps_capacity_reserved_and_other_turns_responsive(fleet):
    user, create, gateways, _runtimes, store, clock = fleet
    release = threading.Event()
    closing, running = Runtime(gate=release), Runtime(hold=True)
    runtimes = [closing, running]
    calls = []

    def factory(options):
        calls.append(options.session_id)
        return AgentSession(runtimes[len(calls) - 1], options)

    gateway = create("alpha", sessions=2, maximum=2, factory=factory)
    sessions = {row["name"]: row["session_id"] for row in user.list_sessions("alpha")}
    first, other = sessions[gateway.options.name + "-1"], sessions[gateway.options.name + "-2"]
    try:
        request = user.close_session(first, idempotency_key="slow-close")
        _until(gateways, closing.entered.is_set)
        with pytest.raises(HubError) as full:
            user.create_session("alpha", "No slot yet", idempotency_key="still-full")
        assert full.value.code == "session_capacity"
        clock[0] += 10
        gateway._last_heartbeat = 0
        turn = user.send_prompt(other, "continue while closing", "parallel")
        _until(gateways, lambda: bool(running.prompts))
        assert store.instances("team")[0]["last_seen"] == clock[0]
        user.cancel_turn(turn["turn_id"])
        _until(gateways, lambda: user.get_turn(turn["turn_id"])["state"] == "interrupted")
        assert user.session_close_request(request["request_id"])["status"] == "queued"
    finally:
        release.set()
    _until(gateways, lambda: user.session_close_request(request["request_id"])["status"] == "closed")
    assert closing.close_calls == 1


def test_close_report_ack_loss_does_not_close_again_or_consume_reclaimed_slot(fleet):
    user, create, gateways, _runtimes, _store, _clock = fleet
    closed, created_runtimes = Runtime(), []

    class LostReport(HubClient):
        reports = 0

        def report_session_close_request(self, **body):
            result = super().report_session_close_request(**body)
            self.reports += 1
            if self.reports == 1:
                raise HubError("ACK lost", code="hub_unavailable")
            return result

    def factory(options):
        runtime = closed if not created_runtimes else Runtime()
        created_runtimes.append(runtime)
        return AgentSession(runtime, options)

    gateway = create("alpha", sessions=1, maximum=1, factory=factory, client_type=LostReport)
    first = user.list_sessions("alpha")[0]["session_id"]
    request = user.close_session(first, idempotency_key="close")
    _until(gateways, lambda: user.session_close_request(request["request_id"])["status"] == "closed")
    second = created(user, gateways, "alpha", "new")
    _until(gateways, lambda: gateway.client.reports >= 2)
    assert closed.close_calls == 1 and second != first and len(created_runtimes) == 2
    turn = user.send_prompt(second, "new slot works", "new-turn")
    _until(gateways, lambda: user.get_turn(turn["turn_id"])["state"] == "completed")


def test_runtime_close_failure_keeps_slot_and_cannot_turn_into_a_false_success(fleet):
    user, create, gateways, _runtimes, _store, _clock = fleet
    runtime = Runtime(fail=True)
    gateway = create("alpha", sessions=1, maximum=1,
                     factory=lambda options: AgentSession(runtime, options))
    first = user.list_sessions("alpha")[0]["session_id"]
    try:
        request = user.close_session(first, idempotency_key="fail")
        _until(gateways, lambda: user.session_close_request(request["request_id"])["status"] == "failed")
        assert user.get_session(first)["status"] == "close_failed"
        for operation in (lambda: user.create_session("alpha", "No slot", idempotency_key="new"),
                          lambda: user.close_session(first, idempotency_key="different")):
            with pytest.raises(HubError):
                operation()
        assert user.close_session(first, idempotency_key="fail")["error_code"] == "session_close_failed"
        gateway.close()
        assert runtime.close_calls == 1
    finally:
        runtime.queue.put(None)  # Release this deliberate failure fixture's event consumer.


def test_close_waits_for_creation_adoption_when_its_ack_is_delayed(fleet):
    user, create, gateways, _runtimes, _store, _clock = fleet
    release, reported, close_seen = threading.Event(), threading.Event(), threading.Event()
    runtime = Runtime()

    class DelayedCreation(HubClient):
        def report_session_request(self, **body):
            result = super().report_session_request(**body)
            reported.set()
            assert release.wait(10)
            return result

        def get_session_close_requests(self, **query):
            result = super().get_session_close_requests(**query)
            if result:
                close_seen.set()
            return result

    create("alpha", client_type=DelayedCreation, factory=lambda options: AgentSession(runtime, options))
    try:
        session_id = created(user, gateways, "alpha", "create-before-close")
        assert reported.is_set()
        request = user.close_session(session_id, idempotency_key="close-before-adopt")
        _until(gateways, close_seen.is_set)
        assert runtime.close_calls == 0
        assert user.session_close_request(request["request_id"])["status"] == "queued"
    finally:
        release.set()
    _until(gateways, lambda: user.session_close_request(request["request_id"])["status"] == "closed")
    assert runtime.close_calls == 1


def test_replacement_generation_loses_queued_close_without_claiming_success(fleet):
    user, create, gateways, _runtimes, _store, clock = fleet
    old = create("alpha", sessions=1)
    session_id = user.list_sessions("alpha")[0]["session_id"]
    old.close()
    request = user.close_session(session_id, idempotency_key="old-close")
    clock[0] += 61
    create("alpha")
    result = user.session_close_request(request["request_id"])
    assert result["status"] == "lost" and result["error_code"] == "owner_replaced"
    assert user.get_session(session_id)["status"] == "close_lost"
    assert user.close_session(session_id, idempotency_key="old-close") == result


def test_cli_recovers_a_lost_close_receipt_with_its_original_key(fleet, monkeypatch, capsys):
    user, create, gateways, _runtimes, _store, _clock = fleet
    create("alpha", sessions=1)
    session_id = user.list_sessions("alpha")[0]["session_id"]

    class LostUserAck(HubClient):
        request_id = ""

        def close_session(self, *args, **kwargs):
            result = super().close_session(*args, **kwargs)
            if not self.request_id:
                self.request_id = result["request_id"]
                raise HubError("PRIVATE ACK lost", code="hub_unavailable")
            assert result["request_id"] == self.request_id
            return result

    client = LostUserAck(user.base_url, "user")
    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *_a, **_k: client)
    args = ["session-close", session_id, "--idempotency-key", "cli-key", "--json"]
    assert hub_cli.hub_main(args) == 2
    assert "PRIVATE" not in capsys.readouterr().err
    assert hub_cli.hub_main(args) == 0
    _until(gateways, lambda: user.session_close_request(client.request_id)["status"] == "closed")
    assert hub_cli.hub_main(["session-close-request", client.request_id, "--json"]) == 0


def test_terminal_event_ack_loss_then_close_does_not_fence_other_sessions(fleet):
    user, create, gateways, _runtimes, _store, _clock = fleet

    class LostTerminalAck(HubClient):
        reports = 0

        def send_event(self, event, **kwargs):
            result = super().send_event(event, **kwargs)
            if event.get("kind") == "turn.completed":
                self.reports += 1
                if self.reports == 1:
                    raise HubError("terminal ACK lost", code="hub_unavailable")
            return result

    gateway = create("alpha", sessions=2, client_type=LostTerminalAck)
    first, other = [row["session_id"] for row in user.list_sessions("alpha")]
    turn = user.send_prompt(first, "finished before close", "first")
    _until(gateways, lambda: user.get_turn(turn["turn_id"])["state"] == "completed")
    assert gateway.client.reports == 1
    request = user.close_session(first, idempotency_key="close-after-lost-event-ack")
    _until(gateways, lambda: user.session_close_request(request["request_id"])["status"] == "closed")
    second = user.send_prompt(other, "keep this instance alive", "other")
    _until(gateways, lambda: user.get_turn(second["turn_id"])["state"] == "completed")
    assert gateway._fatal is None and gateway.client.reports >= 3


def test_late_running_event_after_cancel_and_close_cannot_block_the_outbox(fleet):
    user, create, gateways, runtimes, _store, _clock = fleet
    gateway = create("alpha", sessions=2)
    first, other = [row["session_id"] for row in user.list_sessions("alpha")]
    turn = user.send_prompt(first, "never execute", "cancel-before-close")
    command = gateway.client.get_commands(instance_id="alpha", generation=1)[0]
    gateway._dispatch_command(command)  # Durable running event exists, no Hub start permission yet.
    user.cancel_turn(turn["turn_id"])
    request = user.close_session(first, idempotency_key="close-cancelled-session")
    _until(gateways, lambda: user.session_close_request(request["request_id"])["status"] == "closed")
    second = user.send_prompt(other, "must get past the retired event", "other")
    _until(gateways, lambda: user.get_turn(second["turn_id"])["state"] == "completed")
    assert gateway._fatal is None
    assert all("never execute" not in runtime.prompts for runtime in runtimes["alpha"])
    assert not gateway._store.pending_events()
