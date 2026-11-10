"""Snapshot writes are fenced, atomic and immutable; reads retain history."""

import copy

import pytest

from pbatch.hub_file_snapshot_store import FileSnapshotStore
from pbatch.hub_store import HubStore, HubStoreError
from tests.test_hub_file_snapshot_models import file_row, upload


@pytest.fixture
def snapshot_store(tmp_path):
    clock = [1000.0]
    hub = HubStore(tmp_path / "hub.sqlite", clock=lambda: clock[0])
    hub.register_instance("team", "alpha", {"boot_id": "one"})
    session = hub.upsert_session("team", "alpha", 1, {
        "local_session_id": "local", "controllable": True, "status": "idle"})
    turn = hub.submit_turn("team", session["session_id"], "alice", "work", "one")
    body = upload(session_id=session["session_id"], turn_id=turn["turn_id"])
    yield FileSnapshotStore(hub), hub, body, clock
    hub.close()


def terminal(hub, body, kind="completed", *, cancel=False):
    common = {key: body[key] for key in ("session_id", "turn_id")}
    hub.append_event("team", "alpha", 1, {**common, "event_id": "running",
                     "kind": "turn.running", "payload": {}})
    if cancel:
        hub.cancel_turn("team", body["turn_id"], "alice")
    hub.append_event("team", "alpha", 1, {**common, "event_id": "terminal",
                     "kind": "turn." + kind, "payload": {}})


def test_absent_does_not_mean_empty_success(snapshot_store):
    snapshots, _hub, body, _clock = snapshot_store
    result = snapshots.read("team", body["session_id"], body["turn_id"])
    assert result["state"] == "absent"
    assert result["snapshot_set_id"] is None and result["created_at"] is None
    assert result["files"] == [] and result["verification"] == "content_integrity_only"


def test_atomic_immutable_idempotent_round_trip_and_historical_read(snapshot_store):
    snapshots, hub, body, clock = snapshot_store
    body["files"] += [file_row(b"\xff\x00", "image.bin", "c" * 32), file_row(b"", "empty", "d" * 32)]
    terminal(hub, body)
    saved = snapshots.put("team", body)
    assert snapshots.put("team", copy.deepcopy(body)) == saved
    assert saved["state"] == "ready" and "content_b64" not in saved["files"][0]
    assert [row["content_kind"] for row in saved["files"]] == ["text", "binary", "text"]
    clock[0] += 61
    hub.register_instance("team", "alpha", {"boot_id": "two"})
    assert snapshots.read("team", body["session_id"], body["turn_id"]) == saved
    item = snapshots.read("team", body["session_id"], body["turn_id"], "a" * 32)
    assert item["content_b64"] == body["files"][0]["content_b64"]
    assert item["session_id"] == body["session_id"] and item["snapshot_set_id"] == body["snapshot_set_id"]
    with pytest.raises(HubStoreError, match="generation"):
        snapshots.put("team", body)


@pytest.mark.parametrize("field,value", [("snapshot_set_id", "e" * 32),
                                         ("files", [file_row(b"different")])])
def test_second_batch_or_changed_retry_rejected(snapshot_store, field, value):
    snapshots, hub, body, _clock = snapshot_store
    terminal(hub, body)
    saved = snapshots.put("team", body)
    with pytest.raises(HubStoreError) as caught:
        snapshots.put("team", {**body, field: value})
    assert caught.value.code == "snapshot_conflict"
    assert snapshots.read("team", body["session_id"], body["turn_id"]) == saved


@pytest.mark.parametrize("kind,cancel", [(None, False), ("failed", False), ("completed", True)])
def test_non_success_and_cancel_request_never_publish_files(snapshot_store, kind, cancel):
    snapshots, hub, body, _clock = snapshot_store
    if kind:
        terminal(hub, body, kind, cancel=cancel)
    with pytest.raises(HubStoreError) as caught:
        snapshots.put("team", body)
    assert caught.value.code == "snapshot_turn_state"
    assert snapshots.read("team", body["session_id"], body["turn_id"])["state"] == "absent"


def test_unavailable_and_partial_are_explicit(snapshot_store):
    snapshots, hub, body, _clock = snapshot_store
    terminal(hub, body, "failed")
    body.update(files=[], unavailable=[{"path": "report.txt", "code": "turn_not_completed"}])
    assert snapshots.put("team", body)["state"] == "unavailable"


@pytest.mark.parametrize("change", ["tenant", "instance", "session", "turn", "offline"])
def test_binding_generation_and_tenant_are_enforced(snapshot_store, change):
    snapshots, hub, body, clock = snapshot_store
    terminal(hub, body)
    tenant = "other" if change == "tenant" else "team"
    if change in {"instance", "session", "turn"}:
        body[change + "_id"] = "other"
    if change == "offline":
        clock[0] += 61
    with pytest.raises(HubStoreError):
        snapshots.put(tenant, body)


@pytest.mark.parametrize("target", ["content", "manifest", "digest", "id", "timestamp"])
def test_corrupt_storage_rejected_for_list_and_download(snapshot_store, target):
    snapshots, hub, body, _clock = snapshot_store
    terminal(hub, body)
    snapshots.put("team", body)
    with hub._write() as conn:
        if target == "content":
            conn.execute("UPDATE hub_file_snapshot_bytes SET content=?", (b"tampered",))
        elif target == "manifest":
            conn.execute("UPDATE hub_file_snapshot_sets SET manifest_json='{}'")
        elif target == "id":
            conn.execute("UPDATE hub_file_snapshot_bytes SET snapshot_id=?", ("a" * 100000,))
        elif target == "timestamp":
            conn.execute("UPDATE hub_file_snapshot_sets SET created_at=?", ("x" * 100000,))
        else:
            conn.execute("UPDATE hub_file_snapshot_sets SET payload_digest='wrong'")
    for snapshot_id in (None, "a" * 32):
        with pytest.raises(HubStoreError) as caught:
            snapshots.read("team", body["session_id"], body["turn_id"], snapshot_id)
        assert caught.value.code == "snapshot_integrity"


@pytest.mark.parametrize("limit", ["MAX_INSTANCE_BYTES", "MAX_GLOBAL_BYTES", "MAX_INSTANCE_FILES",
                                  "MAX_GLOBAL_FILES", "MAX_GLOBAL_SETS"])
def test_quota_failure_is_atomic_and_never_evicts(snapshot_store, monkeypatch, limit):
    snapshots, hub, body, _clock = snapshot_store
    terminal(hub, body)
    monkeypatch.setattr("pbatch.hub_file_snapshot_store." + limit, 0)
    with pytest.raises(HubStoreError) as caught:
        snapshots.put("team", body)
    assert caught.value.code == "snapshot_capacity"
    assert snapshots.read("team", body["session_id"], body["turn_id"])["state"] == "absent"
    with hub._connect() as conn:
        assert conn.execute("SELECT count(*) FROM hub_file_snapshot_bytes").fetchone()[0] == 0


def test_concurrent_same_turn_uploads_share_one_immutable_transaction(snapshot_store):
    from concurrent.futures import ThreadPoolExecutor
    snapshots, hub, body, _clock = snapshot_store
    terminal(hub, body)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _index: snapshots.put("team", body), range(8)))
    assert all(value == results[0] for value in results)
    with hub._connect() as conn:
        assert conn.execute("SELECT count(*) FROM hub_file_snapshot_sets").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM hub_file_snapshot_bytes").fetchone()[0] == 1


def test_full_capacity_keeps_earlier_bytes_and_retry_available(snapshot_store, monkeypatch):
    snapshots, hub, body, _clock = snapshot_store
    terminal(hub, body)
    first = snapshots.put("team", body)
    monkeypatch.setattr("pbatch.hub_file_snapshot_store.MAX_GLOBAL_SETS", 1)
    turn = hub.submit_turn("team", body["session_id"], "alice", "next", "two")
    second = {**body, "turn_id": turn["turn_id"], "snapshot_set_id": "e" * 32,
              "files": [], "unavailable": [{"path": "report.txt", "code": "turn_not_completed"}]}
    hub.append_event("team", "alpha", 1, {"session_id": body["session_id"], "turn_id": turn["turn_id"],
                     "event_id": "second-failed", "kind": "turn.failed", "payload": {}})
    with pytest.raises(HubStoreError) as caught:
        snapshots.put("team", second)
    assert caught.value.code == "snapshot_capacity"
    assert snapshots.put("team", body) == first
    assert snapshots.read("team", body["session_id"], body["turn_id"], "a" * 32)["content_b64"]


def test_partial_snapshot_retains_explicit_unavailable_paths(snapshot_store):
    snapshots, hub, body, _clock = snapshot_store
    terminal(hub, body)
    body["unavailable"] = [{"path": "missing.txt", "code": "missing"}]
    result = snapshots.put("team", body)
    assert result["state"] == "partial" and result["unavailable"] == body["unavailable"]
