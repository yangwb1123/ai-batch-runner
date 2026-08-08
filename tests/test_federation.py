"""Tests for D6 联邦最小件：多租户配额 / 跨区域 / 集群 / Gang / 注册证明.

tenant: 并发配额超限拒绝、完成后释放、不同租户互不影响。
region: 注册元数据、status 视图、--region 过滤。
cluster: device_fabric.clusters 解析、集群内选设备。
gang: 原子提交（任一失败全部取消）、全部成功。
attestation: 注册证明字段记录与展示。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from pbatch import fabric, fabric_runner, fabric_server
from pbatch.fabric_rpc import RpcClient


@pytest.fixture()
def control_plane(tmp_path):
    httpd, port = fabric_server.start_server(0, str(tmp_path / "state"),
                                             auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"httpd": httpd, "port": port, "state_dir": tmp_path / "state"}
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _client(control_plane, name, tenant="", region="", attestation=""):
    key = fabric_runner.load_or_create_key(
        str(control_plane["state_dir"]), name)
    client = RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                       key["secret"], key["device_id"])
    assert client.register(name, fabric.local_probe(), tenant=tenant,
                           region=region, attestation=attestation)[
        "approved"] is True
    return client, key


# ---------------------------------------------------------------------------
# 多租户配额
# ---------------------------------------------------------------------------

def test_tenant_quota_limits_concurrent(tmp_path):
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True,
        tenant_quotas={"team-a": 1})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        key = fabric_runner.load_or_create_key(str(tmp_path / "state"),
                                               "node-ta")
        client = RpcClient(f"http://127.0.0.1:{port}", key["secret"],
                           key["device_id"])
        assert client.register("node-ta", fabric.local_probe(),
                               tenant="team-a")["approved"] is True
        ok = client.submit_task(["echo", "1"])
        assert ok["task"] is not None
        rejected = client.submit_task(["echo", "2"])
        assert rejected["task"] is None
        assert "tenant quota" in rejected["error"]
        # 完成后释放配额
        task = ok["task"]
        lease = httpd.control.task_leases[task["task_id"]]
        httpd.control.handle_evidence({
            "device_id": key["device_id"], "lease_id": lease,
            "fencing_token": 1, "evidence": {"result": "passed"},
            "task_id": task["task_id"]})
        ok2 = client.submit_task(["echo", "3"])
        assert ok2["task"] is not None
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_tenant_quota_independent_tenants(tmp_path):
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True,
        tenant_quotas={"team-a": 1, "team-b": 1})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        clients = {}
        for name, tenant in (("node-a", "team-a"), ("node-b", "team-b")):
            key = fabric_runner.load_or_create_key(str(tmp_path / "state"),
                                                   name)
            client = RpcClient(f"http://127.0.0.1:{port}", key["secret"],
                               key["device_id"])
            assert client.register(name, fabric.local_probe(),
                                   tenant=tenant)["approved"] is True
            clients[name] = client
        assert clients["node-a"].submit_task(["echo", "1"])["task"] is not None
        # A 租户满，B 租户不受影响
        assert clients["node-b"].submit_task(["echo", "2"])["task"] is not None
        assert clients["node-a"].submit_task(["echo", "3"])["task"] is None
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 跨区域 / 注册证明
# ---------------------------------------------------------------------------

def test_region_and_attestation_metadata(control_plane):
    client, _ = _client(control_plane, "node-r1", region="cn-east",
                        attestation="sha256:abcd1234")
    from pbatch.fabric_devices import DEVICE_STATE_DIR
    _ = DEVICE_STATE_DIR  # noqa: F841
    device = control_plane["httpd"].control.devices.get(client.device_id, {})
    assert device.get("region") == "cn-east"
    assert device.get("attestation") == "sha256:abcd1234"
    # status 视图带元数据
    view = control_plane["httpd"].control.status_view()
    item = next(d for d in view if d["device_id"] == client.device_id)
    assert item["region"] == "cn-east"
    assert item["attestation"] == "sha256:abcd1234"


def test_schedule_region_filter(tmp_path, monkeypatch):
    """--region 过滤：只选指定区域的设备（跨区域资源池）。"""
    from pbatch import fabric_cmds
    from pbatch.config import DEVICE_FABRIC_CLUSTERS
    _ = DEVICE_FABRIC_CLUSTERS
    import argparse
    # 构造两台不同区域设备
    devices_view = [
        {"name": "cn-east-1", "device_id": "d1", "online": True,
         "cordoned": False, "region": "cn-east",
         "static_capabilities": {"os": "Linux"}, "runtimes": {},
         "capability_valid_until": "2099-01-01T00:00:00"},
        {"name": "cn-west-1", "device_id": "d2", "online": True,
         "cordoned": False, "region": "cn-west",
         "static_capabilities": {"os": "Linux"}, "runtimes": {},
         "capability_valid_until": "2099-01-01T00:00:00"},
    ]
    fake_client = type("C", (), {
        "base_url": "http://x", "secret": "s", "device_id": "d1"})()
    monkeypatch.setattr(fabric_cmds, "_fetch_status",
                        lambda client: (200, {"devices": devices_view}))
    args = argparse.Namespace(
        os=[], arch=[], min_memory_mb=0, min_cpu=0, effect_class="read_only",
        model="", region="cn-east")
    chosen = fabric_cmds._auto_pick_device(args, fake_client)
    assert chosen == "cn-east-1"


# ---------------------------------------------------------------------------
# 集群 / Gang
# ---------------------------------------------------------------------------

def test_config_clusters_parsing():
    from pbatch import config
    assert isinstance(config.DEVICE_FABRIC_CLUSTERS, dict)


def test_gang_atomic_submit_rolls_back_on_failure(tmp_path):
    """Gang：任一提交失败 → 已接受的全部取消（all-or-nothing）。"""
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True, budget_cap=2.0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        keys = {}
        for name in ("node-g1", "node-g2"):
            key = fabric_runner.load_or_create_key(str(tmp_path / "state"),
                                                   name)
            keys[name] = key
            client = RpcClient(f"http://127.0.0.1:{port}", key["secret"],
                               key["device_id"])
            assert client.register(name, fabric.local_probe())[
                "approved"] is True
        # node-g1 先耗光预算 → gang 提交 node-g2 时失败 → node-g1 被取消
        c1 = RpcClient(f"http://127.0.0.1:{port}", keys["node-g1"]["secret"],
                       keys["node-g1"]["device_id"])
        assert c1.submit_task(["echo", "burn"], estimated_cost=2.0)[
            "task"] is not None
        from pbatch import fabric_cmds
        import argparse
        args = argparse.Namespace(
            cmd=["echo", "gang"], cwd="", timeout=30,
            effect_class="read_only", mobility="stateless",
            checkpoint_save="", checkpoint_load="", estimated_cost=1.0,
            speculative=False, gang=True, devices="node-g1,node-g2",
            json=True, model="",
            control=f"http://127.0.0.1:{port}", state_dir=str(tmp_path / "state"),
            device="", as_device="node-g1", cluster="", region="",
            os=[], arch=[], min_memory_mb=0, min_cpu=0)
        from pbatch.fabric_cmds import _control_client as real_control
        monkeypatch_ctx = {"client": None}

        def fake_control(a, acting=""):
            return RpcClient(f"http://127.0.0.1:{port}",
                             keys["node-g1"]["secret"],
                             keys["node-g1"]["device_id"])

        import pbatch.fabric_cmds as mod
        original = mod._control_client
        mod._control_client = fake_control
        try:
            with pytest.raises(SystemExit) as exc:
                mod._cmd_schedule(args)
            assert exc.value.code == 2  # gang 提交失败
        finally:
            mod._control_client = original
        # node-g1 的 burn 任务不受影响；本次 gang 未产生新任务
        gang_tasks = [t for t in httpd.control.tasks.values()
                      if "gang" in " ".join(t.get("argv", []))]
        assert gang_tasks == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_gang_all_accept(tmp_path, monkeypatch):
    """Gang 全部接受：不取消，返回全部 task_id。"""
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        keys = {}
        for name in ("node-ga", "node-gb"):
            key = fabric_runner.load_or_create_key(str(tmp_path / "state"),
                                                   name)
            keys[name] = key
            client = RpcClient(f"http://127.0.0.1:{port}", key["secret"],
                               key["device_id"])
            assert client.register(name, fabric.local_probe())[
                "approved"] is True
        import argparse
        import pbatch.fabric_cmds as mod
        args = argparse.Namespace(
            cmd=["echo", "gang-ok"], cwd="", timeout=30,
            effect_class="read_only", mobility="stateless",
            checkpoint_save="", checkpoint_load="", estimated_cost=0.0,
            speculative=False, gang=True, devices="node-ga,node-gb",
            json=True, model="",
            control=f"http://127.0.0.1:{port}",
            state_dir=str(tmp_path / "state"),
            device="", as_device="node-ga", cluster="", region="",
            os=[], arch=[], min_memory_mb=0, min_cpu=0)

        def fake_control(a, acting=""):
            return RpcClient(f"http://127.0.0.1:{port}",
                             keys["node-ga"]["secret"],
                             keys["node-ga"]["device_id"])

        original = mod._control_client
        mod._control_client = fake_control
        try:
            with pytest.raises(SystemExit) as exc:
                mod._cmd_schedule(args)
            assert exc.value.code == 0
        finally:
            mod._control_client = original
        gang_tasks = [t for t in httpd.control.tasks.values()
                      if "gang-ok" in " ".join(t.get("argv", []))]
        assert len(gang_tasks) == 2  # 两台设备都收到
        assert all(t["status"] == "queued" for t in gang_tasks)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# per-device 并发上限 + 事件 attempt 审计字段
# ---------------------------------------------------------------------------

def test_device_quota_limits_concurrent(tmp_path):
    """D6+ 差值表：per-device 并发上限——进行中超限拒绝，完成后释放。"""
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True,
        device_quotas={"node-dq": 1})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        key = fabric_runner.load_or_create_key(str(tmp_path / "state"),
                                               "node-dq")
        client = RpcClient(f"http://127.0.0.1:{port}", key["secret"],
                           key["device_id"])
        assert client.register("node-dq", fabric.local_probe())[
            "approved"] is True
        first = client.submit_task(["echo", "one"], mobility="stateless")
        assert first["task"] is not None
        second = client.submit_task(["echo", "two"], mobility="stateless")
        assert second["task"] is None
        assert "device quota exhausted" in second["error"]
        # 完成后释放配额 → 可再提交
        task_id = first["task"]["task_id"]
        lease = httpd.control.task_leases[task_id]
        httpd.control.handle_evidence({
            "device_id": key["device_id"], "lease_id": lease,
            "fencing_token": 1, "evidence": {"result": "passed"},
            "task_id": task_id})
        third = client.submit_task(["echo", "three"], mobility="stateless")
        assert third["task"] is not None
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_event_has_attempt_field(tmp_path):
    """审计：submit 事件绑定 attempt（租约编号）。"""
    from pbatch import events
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        key = fabric_runner.load_or_create_key(str(tmp_path / "state"),
                                               "node-ev")
        client = RpcClient(f"http://127.0.0.1:{port}", key["secret"],
                           key["device_id"])
        client.register("node-ev", fabric.local_probe())
        result = client.submit_task(["echo", "x"], mobility="stateless")
        assert result["task"] is not None
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    records = events.read_events(str(tmp_path / "state"), limit=20)
    submits = [r for r in records if r.get("verb") == "submit"]
    assert submits, "submit 事件缺失"
    assert submits[0].get("attempt", ""), "submit 事件缺 attempt 字段"
