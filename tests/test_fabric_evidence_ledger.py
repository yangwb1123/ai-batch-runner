"""ExecutionEvidence digest、设备资源账本与 LOST 尝试语义回归。"""

from __future__ import annotations

import sys
import threading

import pytest

from pbatch import fabric, fabric_server
from pbatch.fabric_ledger import DeviceUsageLedger, UsageLedgerError
from pbatch.fabric_rpc import RpcClient


def test_device_usage_ledger_is_idempotent_and_recoverable(tmp_path):
    ledger = DeviceUsageLedger(tmp_path)
    usage = {"elapsed_seconds": 1.25, "output_bytes": 9}
    first = ledger.record("device-a", usage, task_id="task-a",
                          attempt_id="attempt-a", idempotency_key="key-a")
    duplicate = ledger.record("device-a", usage, task_id="task-a",
                              attempt_id="attempt-a", idempotency_key="key-a")
    assert first["ledger_id"] == duplicate["ledger_id"]
    assert duplicate["duplicate"] is True
    assert ledger.summary("device-a")["records"] == 1
    assert ledger.totals("device-a")["output_bytes"] == 9
    other_device = ledger.record("device-b", usage,
                                 idempotency_key="key-a")
    assert other_device["ledger_id"] != first["ledger_id"]

    restored = DeviceUsageLedger(tmp_path)
    assert restored.summary("device-a")["records"] == 1
    assert restored.totals(task_id="task-a")["elapsed_seconds"] == 1.25
    with pytest.raises(UsageLedgerError):
        restored.record("device-a", {"output_bytes": 10},
                        idempotency_key="key-a")


def test_device_usage_ledger_rejects_capacity_before_append(tmp_path,
                                                            monkeypatch):
    """容量耗尽时不得写入一条使下次启动无法加载的记录。"""
    import pbatch.fabric_ledger as fabric_ledger

    ledger = DeviceUsageLedger(tmp_path)
    ledger.record("device-a", {"output_bytes": 1}, task_id="task-a",
                  attempt_id="attempt-a", idempotency_key="key-a")
    before = ledger.path.read_bytes()
    monkeypatch.setattr(fabric_ledger, "MAX_FILE_BYTES", len(before))

    with pytest.raises(UsageLedgerError, match="capacity exhausted"):
        ledger.record("device-a", {"output_bytes": 2}, task_id="task-b",
                      attempt_id="attempt-b", idempotency_key="key-b")

    assert ledger.path.read_bytes() == before
    assert ledger.summary("device-a")["records"] == 1
    restored = DeviceUsageLedger(tmp_path)
    assert restored.summary("device-a")["records"] == 1
    assert restored.totals("device-a")["output_bytes"] == 1


def test_execution_evidence_serializes_digests_and_input_bytes(tmp_path):
    input_path = tmp_path / "input.txt"
    input_path.write_text("input", encoding="utf-8")
    artifact = fabric.artifact_ref(input_path)
    evidence = fabric.LocalExecutionTarget().execute(fabric.ExecutionSpec(
        argv=[sys.executable, "-c", "print('digest')"],
        code_digest="code-v1", inputs=[artifact]))
    data = evidence.to_dict()
    assert data["code_digest"] == "code-v1"
    assert data["input_digest"]
    assert data["output_digest"]
    assert data["stdout_bytes"] > 0
    assert data["input_bytes"] == artifact.size
    assert data["network_bytes"] == 0


def _server(tmp_path):
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, RpcClient(
        f"http://127.0.0.1:{port}", "secret", "rn-ledger")


def test_control_plane_rejects_tampered_evidence_and_records_usage(tmp_path):
    httpd, thread, client = _server(tmp_path)
    try:
        assert client.register("node-ledger", fabric.local_probe())["approved"] is True
        task = client.submit_task(
            [sys.executable, "-c", "print('ledger')"])["task"]
        evidence = fabric.LocalExecutionTarget().execute(
            fabric.ExecutionSpec(argv=[sys.executable, "-c", "print('ledger')"])
        ).to_dict()
        wrong_environment = dict(evidence)
        wrong_environment["environment_digest"] = "wrong-environment"
        denied = client.evidence(
            task["task_id"], task["lease"]["lease_id"],
            task["lease"]["fencing_token"], wrong_environment,
            task_id=task["task_id"])
        assert denied["accepted"] is False
        assert denied["reason"] == "environment digest mismatch"
        accepted = client.evidence(
            task["task_id"], task["lease"]["lease_id"],
            task["lease"]["fencing_token"], evidence,
            task_id=task["task_id"])
        assert accepted["accepted"] is True
        assert accepted["usage_ledger_id"]

        conflict = dict(evidence)
        conflict["label"] = "tampered-after-execution"
        rejected = client.evidence(
            task["task_id"], task["lease"]["lease_id"],
            task["lease"]["fencing_token"], conflict,
            task_id=task["task_id"])
        assert rejected["accepted"] is False
        assert rejected["reason"] == "evidence conflict"

        status = httpd.control.status_view()
        device = next(item for item in status
                      if item["device_id"] == client.device_id)
        assert device["usage"]["output_bytes"] > 0
        queried = client.usage(task["task_id"])
        assert queried["usage"]["totals"]["output_bytes"] > 0
        assert httpd.control.tasks[task["task_id"]]["attempts"][-1][
            "status"] == "verified"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_network_budget_turns_overrun_into_audited_failure(tmp_path):
    """网络字节硬上限：超限不能把任务标为成功，但仍记账且可幂等重放。"""
    httpd, thread, client = _server(tmp_path)
    try:
        assert client.register("node-network", fabric.local_probe())[
            "approved"] is True
        task = client.submit_task(["echo", "network"],
                                  network_budget_bytes=4)["task"]
        evidence = fabric.LocalExecutionTarget().execute(
            fabric.ExecutionSpec(argv=["echo", "network"])).to_dict()
        evidence["network_bytes"] = 5
        result = client.evidence(
            task["task_id"], task["lease"]["lease_id"],
            task["lease"]["fencing_token"], evidence,
            task_id=task["task_id"])
        assert result["accepted"] is True
        assert result["budget_exceeded"] is True
        stored = httpd.control.tasks[task["task_id"]]
        assert stored["status"] == "failed"
        assert "network budget exceeded" in stored["failure_reason"]
        assert stored["usage"]["network_bytes"] == 5
        duplicate = client.evidence(
            task["task_id"], task["lease"]["lease_id"],
            task["lease"]["fencing_token"], evidence,
            task_id=task["task_id"])
        assert duplicate["accepted"] is True
        assert duplicate["duplicate"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_stale_attempt_is_lost_and_old_lease_cannot_write(tmp_path):
    httpd, thread, client_a = _server(tmp_path)
    client_b = RpcClient(client_a.base_url, "secret-b", "rn-ledger-b")
    try:
        assert client_a.register("node-a")["approved"] is True
        assert client_b.register("node-b")["approved"] is True
        task = client_a.submit_task(["echo", "lost"])["task"]
        old_lease = task["lease"]
        httpd.control._reschedule_stale()
        record = httpd.control.tasks[task["task_id"]]
        assert record["status"] == "queued"
        assert record["device_id"] is None
        assert record["attempts"][-1]["status"] == "lost"
        stale = client_a.evidence(
            task["task_id"], old_lease["lease_id"],
            old_lease["fencing_token"], {"result": "passed"},
            task_id=task["task_id"])
        assert stale["accepted"] is False
        assert "stale" in stale["reason"]

        polled = client_b.poll_tasks()
        assert polled["tasks"] and polled["tasks"][0]["task_id"] == task["task_id"]
        assert len(httpd.control.tasks[task["task_id"]]["attempts"]) == 2
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
