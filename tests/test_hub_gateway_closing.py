"""Close receipts, capacity release, and lifecycle races without a model provider."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from pbatch.hub_client import HubError
from pbatch.hub_gateway import HubGateway
from pbatch.hub_gateway_store import GatewayStore
from pbatch.hub_gateway_closing import _command as validate_close_command
from test_hub_gateway import _options, _wait_for
from test_hub_gateway_creation import CreationClient, Factory, acknowledged


class CloseClient(CreationClient):
    def __init__(self, generation=1):
        super().__init__(generation)
        self.close_commands = []
        self.close_records = {}
        self.close_reports = []
        self.close_ack_lost = False
        self.registration = []

    def register_instance(self, **values):
        self.registration.append(values)
        return super().register_instance(**values)

    def request_close(self, gateway, local_id, key="close-one", **changes):
        row = {"request_id": key, "instance_id": gateway.instance_id,
               "generation": gateway.generation, "local_session_id": local_id,
               "session_id": "hub-" + local_id}
        row.update(changes)
        self.close_records[key] = row
        self.close_commands.append(row)
        return row

    def get_session_close_requests(self, **_values):
        return list(self.close_commands)

    def report_session_close_request(self, **values):
        self.close_reports.append(values)
        row = self.close_records[values["request_id"]]
        self.close_commands = [command for command in self.close_commands
                               if command["request_id"] != values["request_id"]]
        if self.close_ack_lost:
            raise HubError("private acknowledgement failure", code="hub_unavailable")
        return {key: row[key] for key in ("request_id", "instance_id", "session_id")} | {
            "status": values["status"], "error_code": values["error_code"],
            "created_at": 1.0, "updated_at": 2.0}


class CloseProbe:
    def __init__(self, runtime):
        self.original = runtime.close
        self.calls = 0
        self.block = False
        self.fail = False
        self.entered = threading.Event()
        self.release = threading.Event()
        runtime.close = self

    def __call__(self):
        self.calls += 1
        self.entered.set()
        if self.block:
            assert self.release.wait(5), "test did not release runtime close"
        if self.fail:
            raise RuntimeError("private provider close diagnostics")
        self.original()


@pytest.fixture
def closing(tmp_path):
    client, factory = CloseClient(), Factory()
    gateway = HubGateway(_options(tmp_path, max_sessions=1, instance_id="alpha"),
                         client=client, session_factory=factory)
    gateway.start()
    probe = CloseProbe(factory.runtimes[0])
    local_id = factory.sessions[0].state["session_id"]
    yield gateway, client, factory, probe, local_id
    probe.release.set()
    factory.release.set()
    gateway.close()
    probe.original()


def close_acknowledged(gateway, request_id="close-one"):
    row = gateway._store.closure(request_id)
    return row is not None and row["acknowledged"] == 1


def test_close_capability_published_only_after_initial_sessions_are_adopted(closing):
    gateway, client, factory, _, _ = closing
    assert [row["session_close_supported"] for row in client.registration] == [False, True]
    assert client.registration[-1]["session_capacity"] == 1
    assert len(gateway._sessions) == len(factory.sessions) == 1


def test_receipt_blocks_dispatch_before_side_effect_and_success_releases_capacity(closing):
    gateway, client, factory, probe, local_id = closing
    probe.block = True
    client.request_close(gateway, local_id)
    assert probe.entered.wait(2)
    receipt = gateway._store.closure("close-one")
    assert receipt["state"] == "accepted"
    assert gateway._store.session(local_id)["status"] == "closing"
    assert gateway._store.session(local_id)["controllable"] == 0
    assert len(gateway._sessions) == 1
    command = {"turn_id": "late", "local_session_id": local_id,
               "session_id": "hub-" + local_id, "prompt": "cached command"}
    assert gateway._dispatch_command(command) is False
    assert factory.runtimes[0].handles == []
    probe.release.set()
    _wait_for(lambda: close_acknowledged(gateway))
    assert gateway._store.closure("close-one")["state"] == "closed"
    assert gateway._store.session(local_id)["status"] == "closed"
    assert gateway._managed(local_id) is None and probe.calls == 1
    client.request(gateway, "replacement")
    _wait_for(lambda: acknowledged(gateway, "replacement"))
    assert len(gateway._sessions) == 1 and len(factory.sessions) == 2


def test_ack_loss_replays_durable_result_without_another_close_or_hub_command(closing):
    gateway, client, _, probe, local_id = closing
    client.close_ack_lost = True
    client.request_close(gateway, local_id)
    _wait_for(lambda: len(client.close_reports) >= 2)
    assert client.close_commands == []
    assert gateway._store.closure("close-one")["state"] == "closed"
    assert gateway._store.closure("close-one")["acknowledged"] == 0
    assert gateway._managed(local_id) is None and probe.calls == 1
    client.close_ack_lost = False
    _wait_for(lambda: close_acknowledged(gateway))
    assert probe.calls == 1
    assert all(row == client.close_reports[0] for row in client.close_reports)


def test_failed_close_is_permanent_and_cannot_free_capacity_or_be_retried(closing, caplog):
    gateway, client, factory, probe, local_id = closing
    probe.fail = client.close_ack_lost = True
    original = client.request_close(gateway, local_id)
    _wait_for(lambda: len(client.close_reports) >= 2)
    assert factory.sessions[0].state["closed"] is True
    assert factory.runtimes[0].closed is False
    assert gateway._store.session(local_id)["status"] == "close_failed"
    assert gateway._managed(local_id).closing and len(gateway._sessions) == 1
    client.close_ack_lost = False
    _wait_for(lambda: close_acknowledged(gateway))
    client.close_commands.append(original)
    client.request(gateway, "no-slot")
    _wait_for(lambda: acknowledged(gateway, "no-slot"))
    assert gateway._store.creation("no-slot")["error_code"] == "session_capacity"
    assert probe.calls == 1 and len(factory.options) == 1
    with pytest.raises(HubError):
        gateway._closing._receive({**original, "request_id": "another-key"})
    gateway.close()
    assert probe.calls == 1
    assert "private provider" not in caplog.text + repr(client.close_reports)
    assert all(row["error_code"] == "session_close_failed" for row in client.close_reports)


def test_blocked_close_keeps_heartbeat_prompt_cancel_and_creation_responsive(tmp_path):
    client, factory = CloseClient(), Factory()
    gateway = HubGateway(_options(tmp_path, sessions=2, max_sessions=3),
                         client=client, session_factory=factory)
    gateway.start()
    probe = CloseProbe(factory.runtimes[0])
    probe.block = True
    try:
        local_id = factory.sessions[0].state["session_id"]
        client.request_close(gateway, local_id)
        assert probe.entered.wait(2)
        other = factory.sessions[1].state["session_id"]
        factory.runtimes[1].scripts.get_nowait()
        client.commands = [{"turn_id": "other-turn", "local_session_id": other,
                            "session_id": "hub-" + other, "prompt": "work"}]
        gateway._last_heartbeat = gateway._last_poll = 0
        gateway.run_once()
        assert client.heartbeats == 1 and factory.runtimes[1].turn_started.wait(2)
        gateway._dispatch_command({**client.commands[0], "cancel_requested": True})
        assert factory.runtimes[1].interrupts
        client.request(gateway, "third")
        _wait_for(lambda: acknowledged(gateway, "third"))
        assert len(factory.sessions) == 3 and not probe.release.is_set()
    finally:
        probe.release.set()
        gateway.close()


def test_close_waits_for_creation_report_ack_and_local_adoption(tmp_path):
    client, factory = CloseClient(), Factory()
    client.fail_reports = True
    gateway = HubGateway(_options(tmp_path, sessions=0, max_sessions=1),
                         client=client, session_factory=factory)
    try:
        client.request(gateway)
        gateway.start()
        _wait_for(lambda: bool(client.reports))
        probe = CloseProbe(factory.runtimes[0])
        probe.block = True
        client.request_close(gateway, "local-one")
        _wait_for(lambda: gateway._store.closure("close-one") is not None)
        assert gateway._managed("local-one") is None and probe.calls == 0
        client.fail_reports = False
        assert probe.entered.wait(2)
        assert gateway._store.session("local-one")["status"] == "closing"
        assert gateway._store.session("local-one")["controllable"] == 0
        probe.release.set()
        _wait_for(lambda: close_acknowledged(gateway))
        assert acknowledged(gateway, "one") and probe.calls == 1
        assert gateway._managed("local-one") is None
    finally:
        if factory.runtimes:
            probe.release.set()
        gateway.close()


def test_close_waits_for_terminal_turn_thread_to_finish_unwinding(closing, monkeypatch):
    gateway, client, factory, probe, local_id = closing
    entered, release = threading.Event(), threading.Event()
    prompt = factory.sessions[0].prompt

    def unwinding(value):
        result = prompt(value)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(factory.sessions[0], "prompt", unwinding)
    command = {"turn_id": "last-turn", "local_session_id": local_id,
               "session_id": "hub-" + local_id, "prompt": "finish"}
    try:
        assert gateway._dispatch_command(command)
        gateway._retry_pending_start()
        assert entered.wait(2)
        client.request_close(gateway, local_id)
        _wait_for(lambda: gateway._managed(local_id).closing)
        assert gateway._store.turn("last-turn")["state"] == "completed"
        assert probe.calls == 0 and gateway._turn_threads
        release.set()
        _wait_for(lambda: close_acknowledged(gateway))
        assert not gateway._turn_threads and probe.calls == 1
    finally:
        release.set()


def test_late_running_ack_cannot_launch_a_session_already_claimed_for_close(closing, monkeypatch):
    gateway, client, factory, probe, local_id = closing
    entered, release = threading.Event(), threading.Event()
    send = client.send_event

    def late_ack(event, **values):
        entered.set()
        assert release.wait(5)
        return send(event, **values)

    monkeypatch.setattr(client, "send_event", late_ack)
    command = {"turn_id": "delayed", "local_session_id": local_id,
               "session_id": "hub-" + local_id, "prompt": "must not launch"}
    try:
        assert gateway._dispatch_command(command)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(gateway._retry_pending_start)
            assert entered.wait(2)
            client.request_close(gateway, local_id)
            _wait_for(lambda: close_acknowledged(gateway))
            release.set()
            pending.result(timeout=2)
        assert probe.calls == 1 and factory.runtimes[0].handles == []
        assert gateway._pending_starts == {} and gateway._turn_threads == {}
    finally:
        release.set()


@pytest.mark.parametrize("stop", ["shutdown", "fence"])
def test_late_close_completion_does_not_touch_closed_store_or_report(closing, stop):
    gateway, client, _, probe, local_id = closing
    probe.block = True
    client.request_close(gateway, local_id)
    assert probe.entered.wait(2)
    if stop == "fence":
        gateway._fence_and_stop(HubError("fenced", code="stale_generation", status=409))
    started = time.monotonic()
    gateway.close()
    assert time.monotonic() - started < 1.8
    assert probe.calls == 1 and gateway._closed
    probe.release.set()
    _wait_for(lambda: not gateway._closing._thread.is_alive())
    assert client.close_reports == [] and gateway._sessions == {}
    reopened = GatewayStore(gateway.options.state_dir)
    try:
        assert reopened.closure("close-one")["state"] == "accepted"
    finally:
        reopened.close()


def test_late_close_report_ack_does_not_write_after_shutdown(closing, monkeypatch):
    gateway, client, _, probe, local_id = closing
    entered, release = threading.Event(), threading.Event()
    report = client.report_session_close_request

    def blocked_report(**values):
        entered.set()
        assert release.wait(5)
        return report(**values)

    monkeypatch.setattr(client, "report_session_close_request", blocked_report)
    client.request_close(gateway, local_id)
    assert entered.wait(2)
    gateway.close()
    release.set()
    _wait_for(lambda: not gateway._closing._thread.is_alive())
    assert probe.calls == 1
    reopened = GatewayStore(gateway.options.state_dir)
    try:
        assert reopened.closure("close-one")["acknowledged"] == 0
        assert reopened.closure("close-one")["state"] == "closed"
    finally:
        reopened.close()


@pytest.mark.parametrize("when", ["accept", "finish"])
def test_storage_failure_fences_without_false_success_or_repeated_close(closing, monkeypatch, when):
    gateway, client, _, probe, local_id = closing

    def unavailable(*_args, **_kwargs):
        raise OSError("private disk failure")

    method = "accept_closure" if when == "accept" else "finish_closure"
    monkeypatch.setattr(gateway._store, method, unavailable)
    client.request_close(gateway, local_id)
    _wait_for(lambda: gateway._fatal is not None)
    assert gateway._fatal.code == "session_close_unavailable"
    assert "private disk" not in str(gateway._fatal)
    assert client.close_reports == []
    assert probe.calls == (0 if when == "accept" else 1)
    if when == "finish":
        assert len(gateway._sessions) == 1
        gateway.close()
        assert probe.calls == 1


def test_missing_local_session_reports_fixed_unavailable(closing):
    gateway, client, _, probe, _ = closing
    client.request_close(gateway, "not-local")
    _wait_for(lambda: close_acknowledged(gateway))
    assert gateway._store.closure("close-one")["error_code"] == "session_unavailable"
    assert probe.calls == 0 and len(gateway._sessions) == 1


def test_repeated_receipt_requires_same_session_identity(closing):
    gateway, client, _, probe, local_id = closing
    original = client.request_close(gateway, local_id)
    _wait_for(lambda: close_acknowledged(gateway))
    gateway._closing._receive(original)
    with pytest.raises(HubError):
        gateway._closing._receive({**original, "session_id": "different"})
    assert probe.calls == 1


@pytest.mark.parametrize("changes", [{"instance_id": "other"}, {"generation": True},
                                    {"session_id": "wrong"}, {"request_id": ""},
                                    {"path": "/private"}, {"local_session_id": "bad\nvalue"}])
def test_invalid_close_commands_cannot_call_runtime(closing, changes):
    gateway, client, _, probe, local_id = closing
    command = {"request_id": "invalid", "instance_id": gateway.instance_id,
               "generation": gateway.generation, "local_session_id": local_id,
               "session_id": "hub-" + local_id, **changes}
    with pytest.raises(HubError):
        gateway._closing._receive(validate_close_command(command, gateway))
    assert probe.calls == 0 and client.close_reports == []


def test_restart_fences_unacknowledged_close_without_replaying_provider(tmp_path):
    client, factory = CloseClient(), Factory()
    client.close_ack_lost = True
    first = HubGateway(_options(tmp_path), client=client, session_factory=factory)
    first.start()
    local_id = factory.sessions[0].state["session_id"]
    probe = CloseProbe(factory.runtimes[0])
    original = client.request_close(first, local_id)
    _wait_for(lambda: bool(client.close_reports))
    first.close()
    second_client = CloseClient(generation=2)
    second = HubGateway(_options(tmp_path, sessions=0), client=second_client, session_factory=factory)
    try:
        assert second._store.closure("close-one")["state"] == "lost"
        assert second._store.pending_closures(2) == []
        second_client.close_commands.append(original)
        second.start()
        _wait_for(lambda: second._stop.is_set())
        assert probe.calls == 1 and second_client.close_reports == []
    finally:
        second.close()


def test_closure_schema_migrates_existing_receipts_and_indexes_bounded_poll(tmp_path):
    store = GatewayStore(tmp_path)
    store.accept_turn(turn_id="turn", local_session_id="local", prompt_digest="digest")
    store._db.execute("DROP TABLE session_closures")
    store.close()
    reopened = GatewayStore(tmp_path)
    try:
        assert reopened.turn("turn")["prompt_digest"] == "digest"
        assert reopened.pending_closures(1) == []
        plan = reopened._db.execute("""EXPLAIN QUERY PLAN SELECT * FROM session_closures
            WHERE generation=1 AND acknowledged=0 AND state IN ('closed','failed')
            ORDER BY updated_at LIMIT 8""").fetchall()
        assert any("session_closures_pending" in row[3] for row in plan)
    finally:
        reopened.close()


def test_eight_closes_use_one_worker_and_do_not_exceed_bounded_pending_queue(tmp_path):
    client, factory = CloseClient(), Factory()
    gateway = HubGateway(_options(tmp_path, sessions=8, max_sessions=8),
                         client=client, session_factory=factory)
    gateway.start()
    probes = [CloseProbe(runtime) for runtime in factory.runtimes]
    probes[0].block = True
    try:
        with gateway._session_lock:
            for index, session in enumerate(factory.sessions):
                client.request_close(gateway, session.state["session_id"], key=f"close-{index}")
        assert probes[0].entered.wait(2)
        assert len(gateway._closing._commands) == 8
        assert sum(probe.calls for probe in probes) == 1
        gateway._last_heartbeat = 0
        gateway.run_once()
        assert client.heartbeats == 1
        probes[0].release.set()
        _wait_for(lambda: all(close_acknowledged(gateway, f"close-{index}") for index in range(8)))
        assert gateway._sessions == {} and all(probe.calls == 1 for probe in probes)
    finally:
        probes[0].release.set()
        gateway.close()


@pytest.mark.parametrize("changes", [{"session_id": "changed"}, {"status": "failed"},
                                    {"error_code": "injected"}, {"updated_at": float("nan")},
                                    {"actor": "private"}, {"updated_at": 10 ** 1000}])
def test_bad_report_ack_never_recloses_or_marks_receipt_acknowledged(closing, monkeypatch, changes):
    gateway, client, _, probe, local_id = closing
    report = client.report_session_close_request

    def changed_report(**values):
        return {**report(**values), **changes}

    monkeypatch.setattr(client, "report_session_close_request", changed_report)
    client.request_close(gateway, local_id)
    _wait_for(lambda: len(client.close_reports) >= 2)
    assert gateway._store.closure("close-one")["acknowledged"] == 0
    assert gateway._fatal is None and not gateway._stop.is_set()
    assert probe.calls == 1 and gateway._managed(local_id) is None
    monkeypatch.setattr(client, "report_session_close_request", report)
    _wait_for(lambda: close_acknowledged(gateway))
