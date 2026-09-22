"""Creation receipts and worker lifecycle with real AgentSession, no provider service."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pbatch import hub_cli
from pbatch.agent_session import AgentSession
from pbatch.hub_client import HubError
from pbatch.hub_gateway import HubGateway
from pbatch.hub_gateway_config import normalize_config
from pbatch.hub_gateway_store import GatewayStore
from test_agent_session import FakeRuntime
from test_hub_gateway import _FakeClient, _options, _wait_for


class CreationClient(_FakeClient):
    def __init__(self, generation=1):
        super().__init__(generation)
        self.requests = []
        self.records = {}
        self.reports = []
        self.order = []
        self.fail_reports = False
        self.heartbeats = 0
        self.fenced = False

    def register_instance(self, **values):
        self.order.append(("instance", values["session_capacity"]))
        return super().register_instance(**values)

    def register_session(self, **values):
        self.order.append(("initial", values["local_session_id"]))
        return super().register_session(**values)

    def get_session_requests(self, **_values):
        if self.fenced:
            raise HubError("untrusted detail", code="stale_generation", status=409)
        return list(self.requests)

    def heartbeat(self, **values):
        self.heartbeats += 1
        return super().heartbeat(**values)

    def report_session_request(self, **values):
        self.reports.append(values)
        request_id = values["request_id"]
        source = self.records[request_id]
        self.requests = [row for row in self.requests if row["request_id"] != request_id]
        if self.fail_reports:
            raise HubError("acknowledgement lost", code="hub_unavailable")
        return {"request_id": request_id, "instance_id": source["instance_id"],
                "name": source["name"], "status": values["status"],
                "error_code": values["error_code"],
                "session_id": "hub-" + source["local_session_id"] if values["status"] == "created" else ""}

    def request(self, gateway, key="one", **changes):
        row = {"request_id": key, "instance_id": gateway.instance_id,
               "generation": gateway.generation, "name": "Requested " + key,
               "project_id": gateway.options.project_id, "local_session_id": "local-" + key}
        row.update(changes)
        self.records[key] = row
        self.requests.append(row)
        return row


class Factory:
    def __init__(self):
        self.options = []
        self.sessions = []
        self.runtimes = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.fail = False

    def __call__(self, options):
        self.options.append(options)
        if options.session_id:
            self.entered.set()
            if self.block:
                assert self.release.wait(5), "test did not release the factory"
            if self.fail:
                raise RuntimeError("private provider diagnostics")
        runtime = FakeRuntime(capabilities=("interrupt",), scripts=[[("turn.completed", {})]])
        session = AgentSession(runtime, options)
        self.runtimes.append(runtime)
        self.sessions.append(session)
        return session


@pytest.fixture
def creation(tmp_path):
    client, factory = CreationClient(), Factory()
    gateway = HubGateway(_options(tmp_path, sessions=0, max_sessions=1, instance_id="alpha"),
                         client=client, session_factory=factory)
    yield gateway, client, factory
    factory.release.set()
    gateway.close()


def acknowledged(gateway, request_id):
    row = gateway._store.creation(request_id)
    return row is not None and row["acknowledged"] == 1


def test_capacity_is_published_only_after_initial_sessions_are_registered(tmp_path):
    client, factory = CreationClient(), Factory()
    gateway = HubGateway(_options(tmp_path, sessions=2, max_sessions=3),
                         client=client, session_factory=factory)
    try:
        assert client.order == [("instance", 0)]
        gateway.start()
        assert [row[0] for row in client.order] == ["instance", "initial", "initial", "instance"]
        assert client.order[-1] == ("instance", 3)
        assert gateway.generation == gateway._store.generation() == 1
        assert len(factory.sessions) == 2
    finally:
        gateway.close()


@pytest.mark.parametrize("stop", [False, True])
def test_start_is_serialized_and_closing_a_blocked_initial_factory_is_safe(tmp_path, stop):
    client, factory = CreationClient(), Factory()
    entered, release = threading.Event(), threading.Event()

    def blocked(options):
        entered.set()
        assert release.wait(5)
        return factory(options)

    gateway = HubGateway(_options(tmp_path), client=client, session_factory=blocked)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(gateway.start)
            assert entered.wait(2)
            if stop:
                gateway.close()
                release.set()
                with pytest.raises(RuntimeError, match="stopped"):
                    first.result(timeout=2)
                assert factory.runtimes[0].closed and client.registered == []
            else:
                second = executor.submit(gateway.start)
                release.set()
                first.result(timeout=2)
                second.result(timeout=2)
                assert len(factory.options) == len(client.registered) == 1
                assert client.order[-1] == ("instance", 8)
    finally:
        release.set()
        gateway.close()


def test_fixed_id_receipt_precedes_factory_and_operator_settings_are_preserved(creation):
    gateway, client, factory = creation
    factory.block = True
    command = client.request(gateway)
    gateway.start()
    assert factory.entered.wait(2)
    receipt = gateway._store.creation(command["request_id"])
    assert receipt["state"] == "accepted"
    options = factory.options[0]
    assert options.session_id == command["local_session_id"]
    assert options.cwd == gateway.options.cwd and options.workspace_id == gateway.options.project_id
    assert options.agent == gateway.options.agent and options.model == gateway.options.model
    assert options.provider == gateway.options.provider and options.runtime == gateway.options.runtime
    factory.release.set()
    _wait_for(lambda: acknowledged(gateway, "one"))
    assert gateway._managed("local-one").session is factory.sessions[0]
    assert factory.sessions[0].state["session_id"] == "local-one"
    assert client.registered == []


def test_unicode_identifiers_keep_the_existing_character_limit(tmp_path):
    client, factory = CreationClient(), Factory()
    gateway = HubGateway(_options(tmp_path, sessions=0, instance_id="节点" * 100,
                                  project_id="项目" * 100), client=client, session_factory=factory)
    request_id = "请求" * 100
    try:
        command = client.request(gateway, request_id, name="分析", local_session_id="会话" * 100)
        gateway.start()
        _wait_for(lambda: acknowledged(gateway, request_id))
        assert gateway._managed(command["local_session_id"]).session is factory.sessions[0]
    finally:
        gateway.close()


def test_lost_ack_reuses_object_and_retries_without_a_pending_hub_command(creation):
    gateway, client, factory = creation
    client.fail_reports = True
    client.request(gateway)
    gateway.start()
    _wait_for(lambda: bool(client.reports))
    assert client.requests == [] and gateway._managed("local-one") is None
    command = {"turn_id": "turn", "local_session_id": "local-one", "session_id": "hub-local-one",
               "prompt": "execute once", "cancel_requested": False}
    assert gateway._dispatch_command(command) is False
    assert client.events == [] and factory.runtimes[0].handles == []
    _wait_for(lambda: len(client.reports) >= 2)
    assert len(factory.sessions) == 1
    client.fail_reports = False
    _wait_for(lambda: acknowledged(gateway, "one"))
    assert gateway._dispatch_command(command)
    gateway._retry_pending_start()
    _wait_for(lambda: not gateway._turn_threads)
    assert len(factory.runtimes[0].handles) == 1
    assert gateway._dispatch_command(command) is False
    assert len(factory.options) == 1


def test_blocked_creation_does_not_block_heartbeat_prompt_or_cancel(tmp_path):
    client, factory = CreationClient(), Factory()
    gateway = HubGateway(_options(tmp_path), client=client, session_factory=factory)
    try:
        gateway.start()
        initial = factory.sessions[0].state["session_id"]
        factory.runtimes[0].scripts.get_nowait()
        factory.block = True
        client.request(gateway)
        assert factory.entered.wait(2)
        client.commands = [{"turn_id": "turn", "local_session_id": initial,
                            "session_id": "hub-" + initial, "prompt": "work"}]
        gateway._last_heartbeat = 0
        gateway._last_poll = 0
        gateway.run_once()
        assert client.heartbeats == 1 and factory.runtimes[0].turn_started.wait(2)
        gateway._dispatch_command({**client.commands[0], "cancel_requested": True})
        assert factory.runtimes[0].interrupts
        assert not factory.release.is_set() and len(factory.sessions) == 1
    finally:
        factory.release.set()
        gateway.close()


def test_factory_failure_is_fixed_durable_and_does_not_exhaust_capacity(creation, caplog):
    gateway, client, factory = creation
    factory.fail = True
    first = client.request(gateway)
    gateway.start()
    _wait_for(lambda: acknowledged(gateway, "one"))
    row = gateway._store.creation("one")
    assert row["state"] == "failed" and row["error_code"] == "session_creation_failed"
    assert "private provider diagnostics" not in caplog.text + repr(client.reports)
    client.requests.append(first)
    factory.fail = False
    client.request(gateway, "two")
    _wait_for(lambda: acknowledged(gateway, "two"))
    assert len(factory.options) == 2 and len(factory.sessions) == 1


def test_local_capacity_includes_initial_and_inflight_sessions(tmp_path):
    client, factory = CreationClient(), Factory()
    gateway = HubGateway(_options(tmp_path, max_sessions=2), client=client, session_factory=factory)
    try:
        factory.block = True
        client.request(gateway, "one")
        client.request(gateway, "two")
        gateway.start()
        assert factory.entered.wait(2)
        assert gateway._store.creation("two")["error_code"] == "session_capacity"
        factory.release.set()
        _wait_for(lambda: acknowledged(gateway, "one") and acknowledged(gateway, "two"))
        assert len(factory.sessions) == 2
        assert gateway._store.creation("two")["state"] == "failed"
    finally:
        factory.release.set()
        gateway.close()


def test_full_request_queue_uses_one_factory_worker_and_remains_bounded(tmp_path):
    client, factory = CreationClient(), Factory()
    factory.block = True
    gateway = HubGateway(_options(tmp_path, sessions=0), client=client, session_factory=factory)
    for index in range(8):
        client.request(gateway, str(index))
    try:
        gateway.start()
        assert factory.entered.wait(2)
        assert len(factory.options) == 1 and len(gateway._creation._commands) == 8
        gateway._last_heartbeat = 0
        gateway.run_once()
        assert client.heartbeats == 1
        factory.release.set()
        _wait_for(lambda: len(gateway._sessions) == 8)
        assert len(factory.sessions) == 8
        assert all(acknowledged(gateway, str(index)) for index in range(8))
    finally:
        factory.release.set()
        gateway.close()


def test_shutdown_closes_a_factory_result_arriving_after_store_closed(creation):
    gateway, client, factory = creation
    factory.block = True
    client.request(gateway)
    gateway.start()
    assert factory.entered.wait(2)
    gateway.close()
    assert gateway._closed
    factory.release.set()
    _wait_for(lambda: factory.runtimes and factory.runtimes[0].closed)
    _wait_for(lambda: not gateway._creation._thread.is_alive())
    assert client.reports == [] and gateway._sessions == {}


def test_fencing_during_creation_closes_result_without_reporting(creation):
    gateway, client, factory = creation
    factory.block = True
    client.request(gateway)
    gateway.start()
    assert factory.entered.wait(2)
    gateway._fence_and_stop(HubError("fenced", code="stale_generation", status=409))
    factory.release.set()
    _wait_for(lambda: factory.runtimes and factory.runtimes[0].closed)
    assert client.reports == [] and gateway._sessions == {}


def test_shutdown_while_report_is_inflight_closes_object_and_ignores_late_ack(creation, monkeypatch):
    gateway, client, factory = creation
    entered, release = threading.Event(), threading.Event()
    report = client.report_session_request

    def blocked_report(**values):
        entered.set()
        assert release.wait(5)
        return report(**values)

    monkeypatch.setattr(client, "report_session_request", blocked_report)
    client.request(gateway)
    gateway.start()
    assert entered.wait(2)
    try:
        gateway.close()
        assert factory.runtimes[0].closed
    finally:
        release.set()
    _wait_for(lambda: not gateway._creation._thread.is_alive())
    assert gateway._sessions == {}
    reopened = GatewayStore(gateway.options.state_dir)
    try:
        assert reopened.creation("one")["acknowledged"] == 0
    finally:
        reopened.close()


def test_receipt_failure_stops_worker_before_factory_with_fixed_diagnostics(creation, monkeypatch):
    gateway, client, factory = creation

    def failed_write(_command):
        raise OSError("private storage diagnostics")

    monkeypatch.setattr(gateway._store, "accept_creation", failed_write)
    client.request(gateway)
    gateway.start()
    _wait_for(lambda: gateway._fatal is not None)
    assert gateway._fatal.code == "session_creation_unavailable"
    assert "private storage" not in str(gateway._fatal)
    assert factory.options == [] and client.reports == []


def test_changed_command_cannot_replace_an_inflight_receipt(creation):
    gateway, client, factory = creation
    factory.block = True
    original = client.request(gateway)
    gateway.start()
    assert factory.entered.wait(2)
    with pytest.raises(HubError, match="response is invalid"):
        gateway._creation._receive({**original, "name": "changed"})
    factory.release.set()
    _wait_for(lambda: acknowledged(gateway, "one"))
    assert len(factory.options) == 1
    assert gateway._managed("local-one").name == original["name"]


def test_another_request_cannot_reuse_a_managed_local_identity(creation):
    gateway, client, factory = creation
    original = client.request(gateway)
    gateway.start()
    _wait_for(lambda: acknowledged(gateway, "one"))
    with pytest.raises(HubError, match="response is invalid"):
        gateway._creation._receive({**original, "request_id": "different"})
    assert len(factory.options) == 1
    assert gateway._store.creation("different") is None


def test_wrong_provider_session_id_is_closed_and_reported_as_fixed_failure(creation, monkeypatch):
    gateway, client, factory = creation
    original = gateway._factory

    def wrong_identity(options):
        from dataclasses import replace
        return original(replace(options, session_id="wrong"))

    monkeypatch.setattr(gateway, "_factory", wrong_identity)
    client.request(gateway)
    gateway.start()
    _wait_for(lambda: acknowledged(gateway, "one"))
    assert factory.runtimes[0].closed
    assert gateway._store.creation("one")["error_code"] == "session_creation_failed"
    assert gateway._sessions == {}


def test_failed_creation_ack_loss_retries_receipt_without_retrying_factory(creation):
    gateway, client, factory = creation
    factory.fail = client.fail_reports = True
    client.request(gateway)
    gateway.start()
    _wait_for(lambda: len(client.reports) >= 2)
    client.fail_reports = False
    _wait_for(lambda: acknowledged(gateway, "one"))
    assert len(factory.options) == 1
    assert all(row["error_code"] == "session_creation_failed" for row in client.reports)


def test_restart_obsoletes_unacknowledged_creation_and_never_reconstructs(tmp_path):
    client, first_factory = CreationClient(), Factory()
    first = HubGateway(_options(tmp_path, sessions=0), client=client, session_factory=first_factory)
    client.fail_reports = True
    old = client.request(first)
    first.start()
    _wait_for(lambda: bool(client.reports))
    first.close()
    second_factory, second_client = Factory(), CreationClient(generation=2)
    second = HubGateway(_options(tmp_path, sessions=0), client=second_client, session_factory=second_factory)
    try:
        assert second._store.creation("one")["state"] == "lost"
        assert second._store.pending_creations(2) == []
        second_client.requests = [old]
        second.start()
        _wait_for(lambda: second._stop.is_set())
        assert second_factory.options == [] and second_client.reports == []
    finally:
        second.close()


@pytest.mark.parametrize("changes", [{"project_id": "other"}, {"instance_id": "other"},
                                    {"cwd": "/remote"}, {"name": "bad\nname"},
                                    {"local_session_id": ""}, {"generation": True}])
def test_invalid_creation_commands_cannot_start_provider(creation, changes):
    gateway, client, factory = creation
    client.request(gateway, **changes)
    with pytest.raises(HubError):
        gateway._creation._cycle()
    assert factory.options == [] and client.reports == []


@pytest.mark.parametrize("sessions,maximum", [(-1, 8), (2, 1), (0, 0), (0, 9),
                                            (True, 8), (1, True), (0.5, 8), (0, "8")])
def test_gateway_rejects_invalid_session_capacity(tmp_path, sessions, maximum):
    with pytest.raises(ValueError, match="session count"):
        normalize_config(_options(tmp_path, sessions=sessions, max_sessions=maximum))


def test_agent_cli_exposes_zero_initial_and_bounded_total_capacity():
    args = hub_cli._parser().parse_args(["agent", "--sessions", "0", "--max-sessions", "4"])
    assert args.sessions == 0 and args.max_sessions == 4


def test_creation_schema_is_additive_to_an_existing_gateway_store(tmp_path):
    store = GatewayStore(tmp_path)
    store.accept_turn(turn_id="turn", local_session_id="local", prompt_digest="digest")
    store._db.execute("DROP TABLE session_creations")
    store.close()
    reopened = GatewayStore(tmp_path)
    try:
        assert reopened.turn("turn")["prompt_digest"] == "digest"
        assert reopened.pending_creations(1) == []
        query = reopened._db.execute("""EXPLAIN QUERY PLAN SELECT * FROM session_creations
            WHERE generation=1 AND acknowledged=0 AND state IN ('created','failed')
            ORDER BY updated_at LIMIT 8""").fetchall()
        assert any("session_creations_pending" in row[3] for row in query)
    finally:
        reopened.close()
