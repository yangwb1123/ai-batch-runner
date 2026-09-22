"""Idle close requests preserve history and release only proven closed slots."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from pbatch.hub_store import HubStore, HubStoreError
from pbatch.hub_store_helpers import _SCHEMA
from test_hub_session_requests import rejected


@pytest.fixture
def system(tmp_path):
    clock = [1000.0]
    store = HubStore(tmp_path / "hub.sqlite", clock=lambda: clock[0], offline_after=10)
    store.register_instance("team", "alpha", {
        "boot_id": "boot", "session_capacity": 1, "session_close_supported": True})
    session = store.upsert_session("team", "alpha", 1, {
        "local_session_id": "local", "name": "Review", "controllable": True, "status": "idle"})
    yield store, clock, session["session_id"]
    store.close()


def close(store, session, key="key", *, actor="alice", client_id="cli"):
    return store.close_session("team", session, actor, key, client_id=client_id)


def report(store, request, status="closed", error_code="", *, generation=1, instance="alpha"):
    return store.report_session_close_request("team", instance, generation, {
        "request_id": request["request_id"], "status": status, "error_code": error_code})


def create(store, key="create"):
    return store.create_session("team", "alpha", "alice", "Next", key, client_id="cli")


def test_close_freezes_before_ack_and_only_closed_receipt_releases_capacity(system):
    store, clock, session = system
    request = close(store, session)
    assert set(request) == {"request_id", "instance_id", "session_id", "status", "error_code",
                            "created_at", "updated_at"}
    assert request["status"] == "queued" and request["error_code"] == ""
    current = store.session("team", session)
    assert current["status"] == "closing" and current["frozen"] and not current["controllable"]
    rejected("session_capacity", create, store)
    rejected("session_unavailable", store.submit_turn, "team", session, "alice", "Work", "turn")
    rejected("session_read_only", store.upsert_session, "team", "alpha", 1, {
        "local_session_id": "local", "controllable": True, "status": "idle"})
    command, = store.session_close_requests("team", "alpha", 1)
    assert command == {"request_id": request["request_id"], "instance_id": "alpha",
                       "generation": 1, "session_id": session, "local_session_id": "local"}
    clock[0] += 1
    closed = report(store, request)
    assert closed["status"] == "closed" and store.session("team", session)["status"] == "closed"
    clock[0] += 1
    assert report(store, request) == closed == close(store, session)
    assert store.session_close_requests("team", "alpha", 1) == []
    assert create(store)["status"] == "queued"
    rejected("session_capacity", create, store, "another")


@pytest.mark.parametrize("error", ["session_close_failed", "session_unavailable"])
def test_failed_close_stays_frozen_and_retains_slot_without_retry(system, error):
    store, _, session = system
    request = close(store, session)
    failed = report(store, request, "failed", error)
    assert failed["error_code"] == error and failed["status"] == "failed"
    assert report(store, request, "failed", error) == failed == close(store, session)
    current = store.session("team", session)
    assert current["status"] == "close_failed" and current["frozen"]
    rejected("session_capacity", create, store)
    rejected("session_close_failed", close, store, session, "new-key")
    rejected("session_request_terminal", report, store, request)
    assert store.session_close_requests("team", "alpha", 1) == []


def test_request_key_and_session_uniqueness_are_independent(system):
    store, _, session = system
    queued = close(store, session)
    assert close(store, session) == queued
    rejected("idempotency_conflict", close, store, "another-session")
    rejected("session_close_in_progress", close, store, session, "another-key")
    rejected("session_close_in_progress", close, store, session, actor="bob")
    rejected("session_close_in_progress", close, store, session, client_id="console")
    report(store, queued)
    rejected("session_closed", close, store, session, "another-key")


def test_concurrent_same_or_different_key_only_admits_one_close(system):
    store, _, session = system
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: close(store, session), range(8)))
    assert len({row["request_id"] for row in results}) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: rejected("session_close_in_progress", close, store,
                                            session, str(index)), range(8)))
    assert len(store.session_close_requests("team", "alpha", 1)) == 1


def test_forged_status_closed_does_not_release_capacity(system):
    store, _, session = system
    store.upsert_session("team", "alpha", 1, {
        "local_session_id": "local", "controllable": True, "status": "closed"})
    rejected("session_capacity", create, store)
    assert close(store, session)["status"] == "queued"


def test_hub_restart_keeps_pending_receipt_and_frozen_session(system):
    store, clock, session = system
    queued = close(store, session)
    commands = store.session_close_requests("team", "alpha", 1)
    store.close()
    reopened = HubStore(store.path, clock=lambda: clock[0], offline_after=10)
    try:
        assert close(reopened, session) == queued
        assert reopened.session_close_requests("team", "alpha", 1) == commands
        assert reopened.session("team", session)["frozen"]
        rejected("session_capacity", create, reopened)
        assert report(reopened, queued)["status"] == "closed"
    finally:
        reopened.close()


def test_takeover_loses_queued_close_without_claiming_success(system):
    store, clock, session = system
    queued = close(store, session)
    clock[0] += 11
    assert close(store, session) == queued
    rejected("instance_offline", report, store, queued)
    store.register_instance("team", "alpha", {
        "boot_id": "new", "session_capacity": 1, "session_close_supported": True})
    lost = store.session_close_request("team", queued["request_id"])
    assert lost["status"] == "lost" and lost["error_code"] == "owner_replaced"
    assert close(store, session) == lost
    assert store.session("team", session)["status"] == "close_lost"
    rejected("session_unavailable", close, store, session, "new-key")
    rejected("stale_generation", report, store, queued)
    rejected("stale_generation", report, store, queued, generation=2)
    assert store.session_close_requests("team", "alpha", 2) == []
    assert create(store)["status"] == "queued"


@pytest.mark.parametrize("state", ["queued", "running", "cancel_requested"])
def test_active_turn_rejects_close_and_terminal_history_remains(system, state):
    store, _, session = system
    turn = store.submit_turn("team", session, "alice", "Keep history", "turn")
    if state != "queued":
        store.append_event("team", "alpha", 1, {"event_id": "running", "session_id": session,
                           "turn_id": turn["turn_id"], "kind": "turn.running", "payload": {}})
    if state == "cancel_requested":
        store.cancel_turn("team", turn["turn_id"], "alice")
    rejected("session_busy", close, store, session)
    assert not store.session("team", session)["frozen"]
    if state == "queued":
        store.cancel_turn("team", turn["turn_id"], "alice")
    else:
        store.append_event("team", "alpha", 1, {"event_id": "done", "session_id": session,
                           "turn_id": turn["turn_id"], "kind": "turn.completed", "payload": {}})
    events = store.events("team", session)
    report(store, close(store, session))
    assert store.events("team", session) == events
    assert store.turn("team", turn["turn_id"])["status"] in {"completed", "cancelled"}


def test_legacy_database_migration_defaults_unsupported_and_capacity_zero_can_close(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO hub_instances VALUES('team','alpha','boot',1,1000,'{}')")
        conn.execute("PRAGMA user_version = 1")
    store = HubStore(path, clock=lambda: 1000)
    try:
        assert store.instances("team")[0]["session_close_supported"] is False
        session = store.upsert_session("team", "alpha", 1, {
            "local_session_id": "old", "controllable": True})["session_id"]
        rejected("session_close_unsupported", close, store, session)
        store.register_instance("team", "alpha", {
            "boot_id": "boot", "session_close_supported": True})
        assert close(store, session)["status"] == "queued"
    finally:
        store.close()


@pytest.mark.parametrize("value", [1, 0, "true", None, [], {}])
def test_close_capability_must_be_boolean(system, value):
    store, _, _ = system
    with pytest.raises(ValueError):
        store.register_instance("team", "alpha", {"boot_id": "boot", "session_close_supported": value})


@pytest.mark.parametrize("status,error", [("queued", ""), ("lost", "owner_replaced"),
                                        ("closed", "session_close_failed"), ("failed", ""),
                                        ("failed", "secret text"), ("failed", []), (None, "")])
def test_invalid_reports_never_change_pending_state(system, status, error):
    store, _, session = system
    queued = close(store, session)
    with pytest.raises(ValueError):
        report(store, queued, status, error)
    assert store.session_close_request("team", queued["request_id"]) == queued


def test_pending_queue_is_bounded_ordered_and_close_does_not_rewrite_creation_result(system):
    store, _, session = system
    store.register_instance("team", "alpha", {
        "boot_id": "boot", "session_close_supported": True})
    requests = [close(store, session)]
    for index in range(10):
        target = store.upsert_session("team", "alpha", 1, {
            "local_session_id": str(index), "controllable": True})["session_id"]
        requests.append(close(store, target, str(index)))
    pending = store.session_close_requests("team", "alpha", 1)
    assert [row["request_id"] for row in pending] == [row["request_id"] for row in requests[:8]]
    for row in requests[:8]:
        report(store, row)
    assert [row["request_id"] for row in store.session_close_requests("team", "alpha", 1)] == [
        row["request_id"] for row in requests[8:]]
    store.register_instance("team", "alpha", {
        "boot_id": "boot", "session_capacity": 4, "session_close_supported": True})
    created = create(store)
    created = store.report_session_request("team", "alpha", 1, {
        "request_id": created["request_id"], "status": "created"})
    report(store, close(store, created["session_id"], "created-close"))
    assert store.session_request("team", created["request_id"]) == created == create(store)


def test_close_keys_are_separated_by_subject_client_and_tenant(system):
    store, _, session = system
    first = close(store, session)
    store.register_instance("team", "alpha", {
        "boot_id": "boot", "session_close_supported": True})
    for index, actor, client in ((1, "bob", "cli"), (2, "alice", "console")):
        target = store.upsert_session("team", "alpha", 1, {
            "local_session_id": str(index), "controllable": True})["session_id"]
        assert close(store, target, actor=actor, client_id=client)["request_id"] != first["request_id"]
    rejected("not_found", store.session_close_request, "other", first["request_id"])
    rejected("not_found", store.close_session, "other", session, "alice", "key", client_id="cli")


def test_successful_close_cannot_be_revived_and_takeover_preserves_final_result(system):
    store, clock, session = system
    closed = report(store, close(store, session))
    rejected("session_read_only", store.upsert_session, "team", "alpha", 1, {
        "local_session_id": "local", "controllable": True, "status": "idle"})
    clock[0] += 11
    store.register_instance("team", "alpha", {
        "boot_id": "new", "session_capacity": 1, "session_close_supported": True})
    assert store.session_close_request("team", closed["request_id"]) == closed
    assert store.session("team", session)["status"] == "closed"
    rejected("stale_generation", report, store, closed)
    rejected("stale_generation", report, store, closed, generation=2)


def test_success_ack_and_new_reservations_cannot_double_release_capacity(system):
    store, _, session = system
    queued = close(store, session)
    report(store, queued)

    def reserve(index):
        report(store, queued)
        try:
            return create(store, str(index))["status"]
        except HubStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert results.count("queued") == 1 and results.count("session_capacity") == 7


def test_frozen_noncontrollable_or_offline_session_cannot_admit_close(system):
    store, clock, session = system
    store.upsert_session("team", "alpha", 1, {"local_session_id": "local", "controllable": False})
    rejected("session_unavailable", close, store, session)
    store.upsert_session("team", "alpha", 1, {"local_session_id": "local", "controllable": True})
    clock[0] += 11
    rejected("instance_offline", close, store, session)
    store.register_instance("team", "alpha", {
        "boot_id": "new", "session_capacity": 1, "session_close_supported": True})
    rejected("session_unavailable", close, store, session)


@pytest.mark.parametrize("status", ["queued", "closed", "failed"])
def test_terminal_event_ack_replays_after_close_without_reopening_history(system, status):
    store, clock, session = system
    turn = store.submit_turn("team", session, "alice", "Work", "turn")
    body = {"event_id": "done", "session_id": session, "turn_id": turn["turn_id"],
            "kind": "turn.completed", "payload": {}}
    store.append_event("team", "alpha", 1, {**body, "event_id": "start", "kind": "turn.running"})
    original = store.append_event("team", "alpha", 1, body)
    queued = close(store, session)
    if status != "queued":
        report(store, queued, status, "session_close_failed" if status == "failed" else "")
    events = store.events("team", session)
    replay = store.append_event("team", "alpha", 1, body)
    assert replay == {**original, "duplicate": True}
    rejected("session_read_only", store.append_event, "team", "alpha", 1, {**body, "event_id": "new"})
    rejected("event_conflict", store.append_event, "team", "alpha", 1, {**body, "payload": {"new": 1}})
    store.register_instance("team", "beta", {"boot_id": "boot"})
    rejected("stale_generation", store.append_event, "team", "beta", 1, body)
    assert store.events("team", session) == events
    clock[0] += 11
    store.register_instance("team", "alpha", {"boot_id": "new", "session_close_supported": True})
    rejected("stale_generation", store.append_event, "team", "alpha", 1, body)
    rejected("stale_generation", store.append_event, "team", "alpha", 2, body)
