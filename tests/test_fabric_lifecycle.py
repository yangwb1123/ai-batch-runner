"""设备生命周期状态机与控制平面状态转移回归。"""

from __future__ import annotations

import sys
import threading

import pytest

from pbatch import fabric, fabric_server
from pbatch.fabric_lifecycle import DeviceLifecycleStore, LifecycleError
from pbatch.fabric_rpc import RpcClient


def test_lifecycle_store_rejects_illegal_transition_and_recovers(tmp_path):
    store = DeviceLifecycleStore(tmp_path)
    assert store.current("device-a") == "unregistered"
    store.transition("device-a", "enrolled", reason="register")
    store.transition("device-a", "probing", reason="probe")
    store.transition("device-a", "ready", reason="approved")
    with pytest.raises(LifecycleError):
        store.transition("device-a", "enrolled", reason="wrong actor")
    assert store.current("device-a") == "ready"

    restored = DeviceLifecycleStore(tmp_path)
    assert restored.current("device-a") == "ready"
    assert [event["to"] for event in restored.recent(10)] == [
        "enrolled", "probing", "ready"]


def test_lifecycle_store_rejects_capacity_before_append(tmp_path, monkeypatch):
    """容量越界的转移必须在修改日志和内存状态之前失败。"""
    import pbatch.fabric_lifecycle as fabric_lifecycle

    store = DeviceLifecycleStore(tmp_path)
    store.transition("device-a", "enrolled", reason="register")
    before = store.path.read_bytes()
    monkeypatch.setattr(fabric_lifecycle, "MAX_FILE_BYTES", len(before))

    with pytest.raises(LifecycleError, match="capacity exhausted"):
        store.transition("device-a", "ready", reason="approved")

    assert store.path.read_bytes() == before
    assert store.current("device-a") == "enrolled"
    restored = DeviceLifecycleStore(tmp_path)
    assert restored.current("device-a") == "enrolled"
    assert [event["to"] for event in restored.recent(10)] == ["enrolled"]


def test_lifecycle_path_preflights_before_writing(tmp_path):
    store = DeviceLifecycleStore(tmp_path)
    store.transition("device-path", "enrolled")
    with pytest.raises(LifecycleError):
        store.transition_path("device-path", ("ready", "enrolled"))
    assert store.current("device-path") == "enrolled"
    assert [event["to"] for event in store.recent(10)] == ["enrolled"]
    assert store.transition_path(
        "device-path", ("probing", "ready", "reserved")) == "reserved"


def test_control_plane_exposes_lifecycle_transitions(tmp_path):
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = RpcClient(f"http://127.0.0.1:{port}", "secret", "rn-life")
    try:
        registered = client.register("node-life", fabric.local_probe())
        assert registered["approved"] is True
        assert client.heartbeat({}, [])["ok"] is True
        assert client.lifecycle()["lifecycle"]["state"] == "ready"

        assert client.cordon()["ok"] is True
        assert client.lifecycle()["lifecycle"]["state"] == "cordoned"
        assert client.cordon(undo=True)["ok"] is True
        assert client.lifecycle()["lifecycle"]["state"] == "ready"

        task = client.submit_task(
            [sys.executable, "-c", "print('lifecycle')"])["task"]
        assert httpd.control.lifecycle.current(client.device_id) == "reserved"
        assert client.cordon(drain=True)["ok"] is True
        assert client.poll_tasks()["tasks"] == []
        assert client.cordon(undo=True)["ok"] is True
        polled = client.poll_tasks()
        assert polled["tasks"]
        assert httpd.control.lifecycle.current(client.device_id) == "busy"
        evidence = fabric.LocalExecutionTarget().execute(
            fabric.ExecutionSpec(argv=[sys.executable, "-c",
                                       "print('lifecycle')"])).to_dict()
        result = client.evidence(
            task["task_id"], task["lease"]["lease_id"],
            task["lease"]["fencing_token"], evidence,
            task_id=task["task_id"])
        assert result["accepted"] is True
        assert httpd.control.lifecycle.current(client.device_id) == "ready"
        events = client.lifecycle(100)["lifecycle"]["events"]
        assert any(event["to"] == "busy" for event in events)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
