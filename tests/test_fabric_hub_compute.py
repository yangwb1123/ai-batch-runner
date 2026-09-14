"""Hub-facing compute contract tests over the real signed Fabric HTTP server."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from pbatch import fabric, fabric_runner
from pbatch.fabric_rpc import RpcClient
from pbatch.fabric_runner_monitor import TaskMonitor


def _submit(client, request_id, resources=None, context=None, argv=None):
    return client.submit_task(
        argv or ["echo", request_id], timeout=20,
        request_id=request_id, resources=resources,
        hub_context=context or {})


def _wait_for(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    assert predicate(), "condition did not become true before timeout"


def test_inventory_is_signed_bounded_and_only_returns_requested_approved(fabric_http):
    client, _key = fabric_http["register"]("hub-inventory")
    result = client.inventory([client.device_id, "unknown-device"])

    assert result["capabilities"] == [
        "hub.compute.v1", "hub.compute.gpu.v1",
        "hub.compute.workspace.v1"]
    assert result["server_time"] > 0
    assert [row["device_id"] for row in result["devices"]] == [client.device_id]
    row = result["devices"][0]
    assert row["lifecycle_state"] in {"ready", "reserved", "busy"}
    assert row["dynamic_state"]["status"] == "online"
    assert row["dynamic_state"]["available_memory_bytes"] > 0
    assert row["resources"]["cpu_cores"] == row["static_capabilities"]["cpu_cores"]
    assert row["resources"]["memory_bytes"] > 0


def test_resource_admission_is_atomic_and_zero_memory_is_valid(fabric_http):
    client, _key = fabric_http["register"]("hub-resource-race")
    cores = fabric.local_probe()["static_capabilities"]["cpu_cores"]
    barrier = threading.Barrier(3)
    results = []

    def submit(index):
        barrier.wait()
        results.append(_submit(client, f"race-{index}",
                               {"cpu_cores": cores, "memory_bytes": 0}))

    workers = [threading.Thread(target=submit, args=(index,))
               for index in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=5)

    accepted = [item for item in results if item.get("task")]
    rejected = [item for item in results if not item.get("task")]
    assert len(accepted) == len(rejected) == 1
    assert accepted[0]["task"]["mobility"] == "pinned"
    assert accepted[0]["task"]["timeout"] == 20
    assert accepted[0]["task"]["resources"]["memory_bytes"] == 0
    assert rejected[0]["code"] == "resource_busy"
    assert rejected[0]["retryable"] is True


def test_hub_resource_requests_reserve_one_execution_slot_until_terminal(fabric_http):
    client, _key = fabric_http["register"]("hub-single-slot")
    control = fabric_http["httpd"].control
    resources = {"cpu_cores": 1, "memory_bytes": 0}
    first = _submit(client, "slot-first", resources)["task"]
    inventory = client.inventory([client.device_id])["devices"][0]
    assert inventory["execution_slots"] == 1
    assert inventory["reserved_execution_slots"] == 1

    lease_count = len(control.leases)
    second = _submit(client, "slot-second", resources)
    assert second["task"] is None
    assert second["code"] == "resource_busy"
    assert second["retryable"] is True
    assert len(control.tasks) == 1 and len(control.leases) == lease_count

    polled = client.poll_tasks(limit=1)["tasks"]
    assert len(polled) == 1 and polled[0]["task_id"] == first["task_id"]
    lease = polled[0]["lease"]
    assert client.task_start(first["task_id"], lease)["ok"] is True
    evidence = client.evidence(
        first["task_id"], lease["lease_id"], lease["fencing_token"],
        {"result": "passed"}, task_id=first["task_id"])
    assert evidence["accepted"] is True
    assert control.tasks[first["task_id"]]["status"] == "done"

    inventory = client.inventory([client.device_id])["devices"][0]
    assert inventory["reserved_execution_slots"] == 0
    third = _submit(client, "slot-third", resources)
    assert third["task"] is not None
    assert len(control.tasks) == 2


def test_submit_lost_ack_retry_after_restart_reuses_persisted_task(fabric_http):
    httpd, state_dir = fabric_http["httpd"], fabric_http["state_dir"]
    client, _key = fabric_http["register"]("hub-idempotent")
    original_post = client._post
    lose_ack = [True]

    def drop_first_submit_ack(path, payload, timeout=5.0):
        status, body = original_post(path, payload, timeout)
        if path == "/task/submit" and lose_ack[0]:
            lose_ack[0] = False
            return 0, {"error": "simulated lost acknowledgement"}
        return status, body

    client._post = drop_first_submit_ack
    first = _submit(client, "lost-ack", {"cpu_cores": 1, "memory_bytes": 0})
    assert first["task"] is None
    assert first["status_code"] == 0 and first["transport_error"] is True
    client._post = original_post
    committed = list(httpd.control.tasks.values())
    assert len(committed) == 1
    task_id = committed[0]["task_id"]

    from pbatch.fabric_server import ControlPlane
    httpd.control = ControlPlane(str(state_dir), auto_approve=True)
    replay = _submit(client, "lost-ack", {"cpu_cores": 1, "memory_bytes": 0})

    assert replay["duplicate"] is True
    assert replay["task"]["task_id"] == task_id
    assert replay["task"]["status"] == "lost"
    assert len(httpd.control.tasks) == 1
    conflict = _submit(client, "lost-ack", {"cpu_cores": 2, "memory_bytes": 0})
    assert conflict["code"] == "idempotency_conflict"
    assert conflict["retryable"] is False


def test_running_task_monitor_keeps_heartbeat_and_status_polling():
    class Client:
        def __init__(self):
            self.beats = []
            self.statuses = 0
            self.seen = threading.Event()

        def heartbeat(self, state, attempts):
            self.beats.append((state, attempts))
            self.seen.set()
            return {"ok": True}

        def task_status(self, _task_id):
            self.statuses += 1
            return {"task": {"cancel_requested": False}}

    client = Client()
    monitor = TaskMonitor(client, "task-long", threading.Event(), interval=0.02)
    monitor.start()
    try:
        assert client.seen.wait(1)
        _wait_for(lambda: len(client.beats) >= 3 and client.statuses >= 3)
        assert all(state["running_tasks"] == 1 and ids == ["task-long"]
                   for state, ids in client.beats)
    finally:
        monitor.stop()


def test_cancel_kills_running_process_and_releases_quota(fabric_http,
                                                         monkeypatch,
                                                         tmp_path):
    client, _key = fabric_http["register"]("hub-cancel")
    control = fabric_http["httpd"].control
    control.tenant_quotas["default"] = 1
    control.device_quotas[client.device_id] = 1

    class FastMonitor(TaskMonitor):
        def __init__(self, rpc, task_id, event):
            super().__init__(rpc, task_id, event, interval=0.02)

    monkeypatch.setattr(fabric_runner, "TaskMonitor", FastMonitor)
    pid_file, completion_file = tmp_path / "pid", tmp_path / "completed"
    code = ("import os,time; from pathlib import Path; "
            f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
            "time.sleep(10); "
            f"Path({str(completion_file)!r}).write_text('done')")
    task = _submit(client, "cancel-real-process",
                   {"cpu_cores": 1, "memory_bytes": 0},
                   argv=[sys.executable, "-c", code])["task"]
    leased = client.poll_tasks(limit=1)["tasks"][0]
    result = []
    worker = threading.Thread(
        target=lambda: result.append(fabric_runner.execute_task(client, leased)))
    worker.start()
    _wait_for(pid_file.exists)
    assert client.task_status(task["task_id"])["task"]["status"] == "running"
    cancelled = client.task_cancel(task["task_id"])
    assert cancelled["cancel_requested"] is True
    worker.join(timeout=6)

    record = control.tasks[task["task_id"]]
    assert not worker.is_alive()
    assert result[0]["accepted"] is True
    assert record["status"] == "cancelled"
    assert record["evidence"]["cancelled"] is True
    assert not completion_file.exists()
    assert control.tenant_running["default"] == 0
    assert control.device_running[client.device_id] == 0


def test_hub_transport_requires_https_or_literal_loopback():
    RpcClient("http://127.0.0.1:1234", "s", "runner",
              require_secure_transport=True)
    RpcClient("https://fabric.example", "s", "runner",
              require_secure_transport=True)
    for url in ("http://localhost:1234", "http://fabric.example",
                "http://user:pass@127.0.0.1:1234", "https://user@fabric.example"):
        with pytest.raises(ValueError, match="require HTTPS"):
            RpcClient(url, "s", "runner", require_secure_transport=True)
