"""设备织网任务级 Capability Grant 与数据级别策略回归。"""

from __future__ import annotations

import threading

from pbatch import capabilities, fabric_runner, fabric_server
from pbatch.fabric_rpc import RpcClient


def test_device_grant_binds_task_and_data_class():
    grant = capabilities.CapabilityGrant.from_dict({
        "grantId": "g-1", "agentId": "device-1", "nodeId": "device-1",
        "taskNodeId": "task-1", "deviceId": "device-1",
        "allowedActions": ["execute"], "allowedResources": ["/srv/app"],
        "environment": "untrusted-external",
        "allowedDataClasses": ["internal"],
        "issuedAt": "2026-01-01T00:00:00Z",
        "expiresAt": "2099-01-01T00:00:00Z",
        "maxEffectClass": "read_only", "capability": "device.execute",
    })
    allowed, reasons = capabilities.authorize_grant(
        {"id": "device.execute", "domain": "ops", "risk": "low"},
        grant, 1, "device-1", "device-1", "execute", "/srv/app",
        "untrusted-external", "read_only", data_class="internal",
        device_id="device-1", task_node_id="task-1")
    assert allowed is True and reasons == []
    denied, reasons = capabilities.authorize_grant(
        {"id": "device.execute", "domain": "ops", "risk": "low"},
        grant, 1, "device-1", "device-1", "execute", "/srv/app",
        "untrusted-external", "read_only", data_class="secret",
        device_id="device-1", task_node_id="task-1")
    assert denied is False and any("数据级别" in reason for reason in reasons)


def test_server_issues_scoped_grant_and_rejects_policy_escape(tmp_path):
    policies = {"node-sec": {
        "trust_zone": "trusted-local", "trust_level": 3,
        "max_effect_class": "sandboxed",
        "allowed_data_classes": ["public", "internal"],
    }}
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True,
        device_policies=policies)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        client = RpcClient(f"http://127.0.0.1:{port}", "secret", "rn-sec")
        assert client.register("node-sec")["approved"] is True
        accepted = client.submit_task(
            ["echo", "ok"], cwd="/srv/app", effect_class="sandboxed",
            data_class="internal")
        task = accepted["task"]
        assert task["data_class"] == "internal"
        grant = task["lease"]["grant"]
        assert grant["device_id"] == "rn-sec"
        assert grant["task_node_id"] == task["task_id"]
        assert grant["allowed_data_classes"] == ["internal"]
        assert grant["max_effect_class"] == "sandboxed"

        secret = client.submit_task(["echo", "no"], data_class="secret")
        assert secret["task"] is None
        assert "data class" in secret["error"]
        irreversible = client.submit_task(
            ["echo", "no"], effect_class="irreversible")
        assert irreversible["task"] is None
        assert "effect" in irreversible["error"]

        status = httpd.control.status_view()
        record = next(item for item in status if item["device_id"] == "rn-sec")
        assert record["trust_zone"] == "trusted-local"
        assert record["allowed_data_classes"] == ["public", "internal"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_server_rejects_unknown_runner_protocol(tmp_path):
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        client = RpcClient(f"http://127.0.0.1:{port}", "secret", "rn-old")
        result = client.register("node-old", {"protocol_version": "99.0"})
        assert result["approved"] is False
        assert "unsupported protocol" in result["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_runner_does_not_execute_with_mismatched_grant(tmp_path):
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    marker = tmp_path / "must-not-exist"
    try:
        client = RpcClient(f"http://127.0.0.1:{port}", "secret", "rn-run")
        assert client.register("node-run")["approved"] is True
        task = client.submit_task(
            ["python", "-c", f"open({str(marker)!r}, 'w').write('bad')"]
        )["task"]
        task["lease"]["grant"]["task_node_id"] = "tampered"
        result = fabric_runner.execute_task(client, task)
        assert result["accepted"] is True
        assert not marker.exists()
        assert httpd.control.tasks[task["task_id"]]["status"] == "failed"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
