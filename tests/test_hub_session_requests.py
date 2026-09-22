"""Durable session creation: capacity reservations, idempotency and fencing."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from pbatch.hub_store import HubStore, HubStoreError
from pbatch.hub_store_helpers import _SCHEMA, global_session_id


@pytest.fixture
def system(tmp_path):
    clock = [1000.0]
    store = HubStore(tmp_path / "hub.sqlite", clock=lambda: clock[0], offline_after=10)
    store.register_instance("team", "alpha", {
        "boot_id": "boot-a", "project_id": "fixed-project", "session_capacity": 2})
    yield store, clock
    store.close()


def create(store, key="key", name="Review", *, actor="alice", client_id="cli", instance="alpha"):
    return store.create_session("team", instance, actor, name, key, client_id=client_id)


def report(store, request, status="created", error_code="", *, generation=1, instance="alpha"):
    return store.report_session_request("team", instance, generation, {
        "request_id": request["request_id"], "status": status, "error_code": error_code})


def rejected(code, fn, *args, **kwargs):
    with pytest.raises(HubStoreError) as error:
        fn(*args, **kwargs)
    assert error.value.code == code


def test_creation_persists_then_atomic_ack_registers_controllable_session(system):
    store, _ = system
    queued = create(store, name="  Review  ")
    assert queued == create(store)
    assert set(queued) == {"request_id", "instance_id", "name", "status", "session_id",
                           "error_code", "created_at", "updated_at"}
    assert queued["name"] == "Review" and queued["status"] == "queued"
    assert queued["session_id"] == queued["error_code"] == ""
    command, = store.session_requests("team", "alpha", 1)
    assert set(command) == {"request_id", "instance_id", "generation", "name",
                            "project_id", "local_session_id"}
    assert command["project_id"] == "fixed-project"
    assert store.sessions("team") == []
    created = report(store, queued)
    expected = global_session_id("team", "alpha", command["local_session_id"])
    assert created["status"] == "created" and created["session_id"] == expected
    session = store.session("team", expected)
    assert session["controllable"] and session["project_id"] == "fixed-project"
    assert session["name"] == "Review"
    assert report(store, queued) == created == create(store)
    assert store.session_requests("team", "alpha", 1) == []
    assert store.submit_turn("team", expected, "alice", "Work", "turn")["status"] == "queued"


def test_queued_request_and_local_identity_survive_hub_restart(system):
    store, clock = system
    queued = create(store)
    commands = store.session_requests("team", "alpha", 1)
    store.close()
    reopened = HubStore(store.path, clock=lambda: clock[0], offline_after=10)
    try:
        assert reopened.session_request("team", queued["request_id"]) == queued
        assert reopened.session_requests("team", "alpha", 1) == commands
        assert create(reopened) == queued
        assert report(reopened, queued)["status"] == "created"
    finally:
        reopened.close()


def test_concurrent_same_key_creates_one_durable_reservation(system):
    store, _ = system
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: create(store), range(8)))
    assert len({row["request_id"] for row in results}) == 1
    assert len(store.session_requests("team", "alpha", 1)) == 1


def test_distinct_concurrent_requests_cannot_overbook_capacity(system):
    store, _ = system
    store.upsert_session("team", "alpha", 1, {"local_session_id": "initial"})

    def submit(index):
        try:
            return create(store, str(index))
        except HubStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert results.count("session_capacity") == 7
    queued = next(row for row in results if isinstance(row, dict))
    rejected("session_capacity", store.upsert_session, "team", "alpha", 1,
             {"local_session_id": "additional"})
    report(store, queued)
    assert len(store.sessions("team")) == 2
    rejected("session_capacity", create, store, "still-full")


def test_completion_and_admission_share_capacity_transaction(system):
    store, _ = system
    first, second = create(store, "first"), create(store, "second")

    def submit():
        rejected("session_capacity", create, store, "third")

    with ThreadPoolExecutor(max_workers=2) as pool:
        done = [pool.submit(report, store, first), pool.submit(submit)]
        for future in done:
            future.result()
    assert len(store.sessions("team")) == 1
    assert store.session_requests("team", "alpha", 1)[0]["request_id"] == second["request_id"]


def test_same_actor_key_is_bound_to_client_instance_and_name(system):
    store, _ = system
    first = create(store)
    rejected("idempotency_conflict", create, store, name="Different")
    rejected("idempotency_conflict", create, store, instance="missing")
    second = create(store, client_id="tui")
    assert first["request_id"] != second["request_id"]
    report(store, second, "failed", "session_creation_failed")
    assert create(store, actor="bob")["request_id"] not in {
        first["request_id"], second["request_id"]}


def test_failure_releases_reservation_and_replay_does_not_change_terminal_state(system):
    store, clock = system
    first = create(store)
    failed = report(store, first, "failed", "session_creation_failed")
    clock[0] += 1
    assert report(store, first, "failed", "session_creation_failed") == failed
    rejected("session_request_terminal", report, store, first)
    rejected("session_request_terminal", report, store, first, "failed", "session_capacity")
    assert create(store) == failed
    create(store, "second")
    create(store, "third")
    assert len(store.session_requests("team", "alpha", 1)) == 2


def test_created_ack_replays_once_and_conflicting_failure_is_rejected(system):
    store, _ = system
    queued = create(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: report(store, queued), range(8)))
    assert len({row["session_id"] for row in results}) == 1
    assert len(store.sessions("team")) == 1
    rejected("session_request_terminal", report, store, queued, "failed", "session_capacity")


def test_ack_failure_rolls_back_session_insert_with_request_still_queued(system):
    store, _ = system
    queued = create(store)
    with store._write() as conn:
        conn.execute("CREATE TRIGGER reject_completion BEFORE UPDATE ON hub_session_requests "
                     "BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        report(store, queued)
    assert store.sessions("team") == []
    assert store.session_request("team", queued["request_id"]) == queued
    with store._write() as conn:
        conn.execute("DROP TRIGGER reject_completion")
    assert report(store, queued)["status"] == "created"


def test_pending_queue_is_bounded_fifo_even_with_equal_timestamps(system):
    store, _ = system
    store.register_instance("team", "alpha", {"boot_id": "boot-a", "session_capacity": 8})
    expected = [create(store, str(index))["request_id"] for index in range(8)]
    assert [row["request_id"] for row in store.session_requests("team", "alpha", 1)] == expected
    rejected("session_capacity", create, store, "overflow")


def test_existing_registered_session_can_refresh_when_capacity_is_full(system):
    store, _ = system
    store.upsert_session("team", "alpha", 1, {"local_session_id": "initial"})
    create(store)
    assert store.upsert_session("team", "alpha", 1, {
        "local_session_id": "initial", "controllable": True})["controllable"]


def test_capacity_cannot_shrink_below_registered_and_reserved_slots(system):
    store, _ = system
    store.upsert_session("team", "alpha", 1, {"local_session_id": "initial"})
    queued = create(store)
    rejected("session_capacity", store.register_instance, "team", "alpha",
             {"boot_id": "boot-a", "session_capacity": 1})
    assert store.instances("team")[0]["session_capacity"] == 2
    report(store, queued, "failed", "session_creation_failed")
    changed = store.register_instance("team", "alpha", {"boot_id": "boot-a", "session_capacity": 1})
    assert changed["session_capacity"] == 1
    assert store.register_instance("team", "alpha", {"boot_id": "boot-a"})["session_capacity"] == 0


def test_offline_replay_and_takeover_lose_only_queued_requests(system):
    store, clock = system
    created = report(store, create(store, "created"))
    queued = create(store)
    clock[0] += 11
    assert create(store) == queued
    rejected("instance_offline", create, store, "new")
    rejected("instance_offline", store.session_requests, "team", "alpha", 1)
    rejected("instance_offline", report, store, queued)
    registration = store.register_instance("team", "alpha", {
        "boot_id": "boot-b", "session_capacity": 2})
    assert registration["generation"] == 2
    lost = store.session_request("team", queued["request_id"])
    assert lost["status"] == "lost" and lost["error_code"] == "owner_replaced"
    assert create(store) == lost
    rejected("stale_generation", report, store, queued)
    rejected("stale_generation", report, store, queued, generation=2)
    assert store.session_request("team", created["request_id"])["status"] == "created"
    assert store.session("team", created["session_id"])["frozen"]
    assert create(store, "new-generation")["status"] == "queued"


def test_cross_tenant_instance_and_reserved_ids_cannot_complete_request(system):
    store, _ = system
    queued = create(store)
    command, = store.session_requests("team", "alpha", 1)
    rejected("not_found", store.session_request, "other", queued["request_id"])
    store.register_instance("team", "beta", {"boot_id": "beta"})
    rejected("not_found", report, store, queued, instance="beta")
    rejected("session_reserved", store.upsert_session, "team", "alpha", 1,
             {"local_session_id": command["local_session_id"]})
    store.register_instance("team", "alpha", {"boot_id": "boot-a"})
    rejected("session_reserved", store.upsert_session, "team", "alpha", 1,
             {"local_session_id": command["local_session_id"]})
    assert report(store, queued)["status"] == "created"


@pytest.mark.parametrize("name", ["", "  ", "bad\nname", "bad\x00name", "bad\x85name",
                                  "é" * 129, 5, None])
def test_creation_rejects_invalid_names(system, name):
    store, _ = system
    with pytest.raises(ValueError):
        create(store, name=name)
    assert store.session_requests("team", "alpha", 1) == []


@pytest.mark.parametrize("value", [True, -1, 9, "2", 2.0, None])
def test_registration_rejects_invalid_capacity(system, value):
    store, _ = system
    with pytest.raises(ValueError):
        store.register_instance("team", "alpha", {"boot_id": "boot-a", "session_capacity": value})
    assert store.instances("team")[0]["session_capacity"] == 2


@pytest.mark.parametrize("status,error_code", [("queued", ""), ("lost", "owner_replaced"),
                                               ("created", "session_capacity"), ("failed", ""),
                                               ("failed", "sensitive upstream diagnostic")])
def test_reports_reject_invalid_states_and_raw_errors(system, status, error_code):
    store, _ = system
    queued = create(store)
    with pytest.raises(ValueError):
        report(store, queued, status, error_code)
    assert store.session_request("team", queued["request_id"]) == queued


def test_additive_schema_keeps_legacy_db_and_unadvertised_gateway_working(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO hub_instances VALUES(?,?,?,?,?,?)",
                     ("team", "alpha", "boot-a", 1, 1000, '{}'))
    store = HubStore(path, clock=lambda: 1000)
    try:
        assert store.instances("team")[0]["session_capacity"] == 0
        rejected("session_creation_unsupported", create, store)
        for index in range(9):
            store.upsert_session("team", "alpha", 1, {"local_session_id": str(index)})
        assert len(store.sessions("team")) == 9
    finally:
        store.close()
