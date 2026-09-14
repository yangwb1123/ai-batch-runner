from concurrent.futures import ThreadPoolExecutor

import pytest

from pbatch.hub_store import HubStore, HubStoreError


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _store(tmp_path, clock=None, offline_after=30):
    return HubStore(tmp_path / "hub.sqlite", clock=clock or Clock(),
                    offline_after=offline_after)


def _session(store, tenant="tenant-a", instance="worker-a", boot="boot-a",
             local="session-1"):
    registered = store.register_instance(tenant, instance, {"boot_id": boot})
    session = store.upsert_session(
        tenant, instance, registered["generation"],
        {"local_session_id": local, "name": local, "project_id": "project-a",
         "controllable": True, "status": "active"})
    return registered, session


def _event(store, instance, generation, session, event_id, kind,
           turn_id="", payload=None, tenant="tenant-a"):
    return store.append_event(tenant, instance, generation, {
        "event_id": event_id, "session_id": session["session_id"],
        "turn_id": turn_id, "kind": kind, "payload": payload or {},
    })


def _submit(store, session, key="request-1", prompt="do the work"):
    return store.submit_turn("tenant-a", session["session_id"], "user-1",
                             prompt, key)


def test_turn_idempotency_private_prompt_and_single_active_turn(tmp_path):
    store = _store(tmp_path)
    _, session = _session(store)
    first = _submit(store, session)
    repeated = _submit(store, session)

    assert first["turn_id"] == repeated["turn_id"]
    assert "prompt" not in store.turn("tenant-a", first["turn_id"])
    command, = store.commands("tenant-a", "worker-a", 1)
    assert command["prompt"] == "do the work"
    assert command["turn_id"] == first["turn_id"]
    assert command["command_id"] == store.commands(
        "tenant-a", "worker-a", 1)[0]["command_id"]
    with pytest.raises(HubStoreError, match="different content"):
        _submit(store, session, prompt="different")
    with pytest.raises(HubStoreError, match="non-terminal"):
        _submit(store, session, key="request-2")
    store.close()


def test_concurrent_duplicate_submissions_create_one_turn(tmp_path):
    store = _store(tmp_path)
    _, session = _session(store)

    def submit(_):
        return _submit(store, session)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert len({item["turn_id"] for item in results}) == 1
    assert len(store.commands("tenant-a", "worker-a", 1)) == 1
    assert len(store.events("tenant-a", session["session_id"])) == 1
    store.close()


def test_same_session_rejects_concurrent_distinct_turns(tmp_path):
    store = _store(tmp_path)
    _, session = _session(store)

    def submit(key):
        try:
            return store.submit_turn("tenant-a", session["session_id"],
                                     "user-1", key, key)
        except HubStoreError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ("request-a", "request-b")))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert [item for item in results if isinstance(item, str)] == ["turn_active"]
    store.close()


def test_tenant_scoping_and_allowlist_filter_before_page_limit(tmp_path):
    store = _store(tmp_path)
    worker_b, _ = _session(store, instance="worker-b", local="b")
    worker_a, session_a = _session(store, instance="worker-a", local="a")
    worker_c, session_c = _session(store, instance="worker-c", local="c")

    assert [item["instance_id"] for item in store.instances(
        "tenant-a", limit=1, allowed_instances=("worker-c",))] == ["worker-c"]
    visible = store.sessions("tenant-a", limit=1, allowed_instances=("worker-c",))
    assert [item["session_id"] for item in visible] == [session_c["session_id"]]
    assert store.instances("tenant-a", allowed_instances=()) == []
    assert store.sessions("tenant-a", allowed_instances=()) == []
    assert store.instances("tenant-b") == []
    with pytest.raises(HubStoreError) as error:
        store.session("tenant-b", session_a["session_id"])
    assert error.value.status == 404
    turn = store.submit_turn("tenant-a", session_a["session_id"],
                             "user-1", "prompt", "tenant-key")
    with pytest.raises(HubStoreError) as error:
        store.turn("tenant-b", turn["turn_id"])
    assert error.value.status == 404
    assert {worker_a["generation"], worker_b["generation"],
            worker_c["generation"]} == {1}
    store.close()


def test_generation_takeover_freezes_sessions_loses_turns_and_fences_old_writer(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock, offline_after=10)
    registered, session = _session(store)
    turn = _submit(store, session)

    with pytest.raises(HubStoreError, match="still active"):
        store.register_instance("tenant-a", "worker-a", {"boot_id": "boot-b"})
    clock.advance(11)
    offline_session = store.session("tenant-a", session["session_id"])
    assert offline_session["online"] is False
    assert offline_session["controllable"] is False

    replacement = store.register_instance(
        "tenant-a", "worker-a", {"boot_id": "boot-b"})
    assert replacement["generation"] == registered["generation"] + 1
    assert store.turn("tenant-a", turn["turn_id"])["status"] == "lost"
    assert [item["kind"] for item in store.events(
        "tenant-a", session["session_id"])] == ["turn.queued", "turn.lost"]
    frozen = store.session("tenant-a", session["session_id"])
    assert frozen["frozen"] is True
    assert frozen["controllable"] is False
    with pytest.raises(HubStoreError, match="stale"):
        store.heartbeat("tenant-a", "worker-a", registered["generation"])
    with pytest.raises(HubStoreError, match="stale"):
        _event(store, "worker-a", registered["generation"], session,
               "old-event", "notice")
    with pytest.raises(HubStoreError, match="read-only"):
        store.upsert_session("tenant-a", "worker-a", replacement["generation"], {
            "local_session_id": "session-1", "controllable": True,
        })
    newer = store.upsert_session("tenant-a", "worker-a", replacement["generation"], {
        "local_session_id": "session-2", "controllable": True,
    })
    assert newer["controllable"] is True
    store.close()


def test_heartbeat_refreshes_online_state_and_stale_gateway_writes_fail(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock, offline_after=5)
    registered, session = _session(store)
    clock.advance(6)
    with pytest.raises(HubStoreError, match="expired"):
        store.commands("tenant-a", "worker-a", registered["generation"])
    assert store.heartbeat("tenant-a", "worker-a", 1)["online"] is True
    assert store.session("tenant-a", session["session_id"])["controllable"] is True
    store.close()


def test_event_idempotency_status_order_and_cursor_survive_restart(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    registered, session = _session(store)
    turn = _submit(store, session)
    first = _event(store, "worker-a", 1, session, "e-1", "turn.running",
                   turn["turn_id"], {"message": "contains token text but no secret key"})
    duplicate = _event(store, "worker-a", 1, session, "e-1", "turn.running",
                       turn["turn_id"], {"message": "contains token text but no secret key"})
    assert duplicate["duplicate"] is True
    assert duplicate["cursor"] == first["cursor"]
    assert store.turn("tenant-a", turn["turn_id"])["status"] == "running"
    with pytest.raises(HubStoreError, match="different content"):
        _event(store, "worker-a", 1, session, "e-1", "turn.running",
               turn["turn_id"], {"message": "changed"})
    with pytest.raises(ValueError, match="sensitive key"):
        _event(store, "worker-a", 1, session, "e-secret", "notice",
               payload={"nested": {"api_key": "not stored"}})
    final = _event(store, "worker-a", 1, session, "e-2", "turn.completed",
                   turn["turn_id"], {"result": "ok"})
    assert final["cursor"] == first["cursor"] + 1
    assert store.turn("tenant-a", turn["turn_id"])["status"] == "completed"
    with pytest.raises(HubStoreError, match="already terminal") as rejected:
        _event(store, "worker-a", 1, session, "e-3", "turn.failed",
               turn["turn_id"])
    assert rejected.value.code == "turn_terminal"
    records = store.events("tenant-a", session["session_id"], after=first["cursor"])
    assert [item["event_id"] for item in records] == ["e-2"]
    store.close()

    reopened = HubStore(tmp_path / "hub.sqlite", clock=clock)
    assert reopened.events("tenant-a", session["session_id"])[-1]["cursor"] == final["cursor"]
    next_event = _event(reopened, "worker-a", 1, session, "e-4", "notice")
    assert next_event["cursor"] == final["cursor"] + 1
    reopened.close()


def test_queued_cancel_is_terminal_and_running_cancel_retries_same_command(tmp_path):
    store = _store(tmp_path)
    _, session = _session(store)
    queued = _submit(store, session)
    cancelled = store.cancel_turn("tenant-a", queued["turn_id"], "user-1")
    assert cancelled["status"] == "cancelled"
    assert store.commands("tenant-a", "worker-a", 1) == []
    assert store.events("tenant-a", session["session_id"])[-1]["kind"] == "turn.cancelled"

    running = _submit(store, session, key="request-2")
    _event(store, "worker-a", 1, session, "start", "turn.running", running["turn_id"])
    requested = store.cancel_turn("tenant-a", running["turn_id"], "user-1")
    repeated = store.cancel_turn("tenant-a", running["turn_id"], "user-2")
    assert requested["status"] == repeated["status"] == "cancel_requested"
    command, = store.commands("tenant-a", "worker-a", 1)
    assert command["cancel_requested"] is True
    assert command["prompt"] == ""
    assert command["turn_id"] == running["turn_id"]
    _event(store, "worker-a", 1, session, "stop", "turn.interrupted", running["turn_id"])
    assert store.turn("tenant-a", running["turn_id"])["status"] == "interrupted"
    assert store.commands("tenant-a", "worker-a", 1) == []
    store.close()


def test_bounds_and_database_permissions(tmp_path):
    store = _store(tmp_path)
    _, session = _session(store)
    with pytest.raises(ValueError, match="limit"):
        store.sessions("tenant-a", limit=201)
    with pytest.raises(ValueError, match="prompt exceeds"):
        _submit(store, session, prompt="x" * (128 * 1024 + 1))
    assert (tmp_path / "hub.sqlite").stat().st_mode & 0o777 == 0o600
    store.close()
    with pytest.raises(HubStoreError, match="closed"):
        store.instances("tenant-a")


def test_one_hub_process_owns_database_lock(tmp_path):
    path = tmp_path / "hub.sqlite"
    store = HubStore(path)
    with pytest.raises(HubStoreError, match="another HubStore"):
        HubStore(path)
    store.close()
    reopened = HubStore(path)
    reopened.close()
