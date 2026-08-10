"""Tests for the device fabric D4: 任务调度与 Runner 执行路由.

覆盖：任务提交→Runner 认领→本地执行→证据回传全流程、证据幂等、
Cordon/Drain 阻断调度、任务迁移（旧 fencing token 失效）、失联设备
任务自动重排队（对账）、取消、CLI schedule 端到端。

回环 + 真实子进程执行（echo/print），无外部网络。
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from pbatch import (fabric, fabric_cli, fabric_runner, fabric_server,
                             fabric_server_cli)
from pbatch.fabric_rpc import RpcClient


@pytest.fixture()
def control_plane(tmp_path):
    """控制平面（auto-approve，随机端口）。"""
    httpd, port = fabric_server.start_server(0, str(tmp_path / "state"),
                                             auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"httpd": httpd, "port": port, "state_dir": tmp_path / "state"}
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _base(control_plane):
    return f"http://127.0.0.1:{control_plane['port']}"


def _client(control_plane, secret, device_id):
    return RpcClient(_base(control_plane), secret, device_id)


def _start_runner(control_plane, name, state_dir):
    """启动 Runner 线程（短心跳），返回 (thread, stop_event)。"""
    stop = threading.Event()
    thread = threading.Thread(
        target=fabric_runner.run_runner_loop,
        args=(_base(control_plane), name, str(state_dir)),
        kwargs={"heartbeat_interval": 0.05, "stop_event": stop},
        daemon=True)
    thread.start()
    return thread, stop


def _wait_task(control_plane, task_id, statuses, timeout=10.0):
    """轮询任务直到状态进入目标集合（默认 done/failed）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = control_plane["httpd"].control.tasks.get(task_id, {})
        if task.get("status") in statuses:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} not in {statuses}: "
                         f"{control_plane['httpd'].control.tasks.get(task_id)}")



def _register(control_plane, key, name):
    """注册设备（auto-approve 直接通过），返回 client。"""
    client = _client(control_plane, key["secret"], key["device_id"])
    assert client.register(name, fabric.local_probe())["approved"] is True
    return client


@pytest.mark.scenario
def test_task_full_flow_via_runner(control_plane):
    """提交 → Runner 认领 → 本地执行 → 证据回传（带 fencing token）。"""
    state = control_plane["state_dir"]
    key = fabric_runner.load_or_create_key(str(state), "node-b")
    client = _register(control_plane, key, "node-b")
    thread, stop = _start_runner(control_plane, "node-b", state)
    try:
        time.sleep(0.2)  # 等注册 + 心跳
        submitted = client.submit_task(
            [sys.executable, "-c", "print('hello from runner')"],
            timeout=30, mobility="stateless")
        task = submitted["task"]
        assert task["status"] == "queued"
        done = _wait_task(control_plane, task["task_id"], ("done", "failed"))
        assert done["status"] == "done"
        evidence = done["evidence"]
        assert evidence["result"] == "passed"
        assert "hello from runner" in evidence["stdout"]
        # 幂等：重复提交同一任务证据 → accepted（duplicate）
        view = client.task_status(task["task_id"])["task"]
        lease = view["lease"]
        again = client.evidence(task["task_id"], lease["lease_id"],
                                lease["fencing_token"], evidence,
                                task_id=task["task_id"])
        assert again["accepted"] is True and again.get("duplicate") is True
    finally:
        stop.set()
        thread.join(timeout=5)


def test_task_failure_reported(control_plane):
    """非零退出 → 任务 failed + 证据携带 exit_code。"""
    state = control_plane["state_dir"]
    key = fabric_runner.load_or_create_key(str(state), "node-f")
    client = _register(control_plane, key, "node-f")
    thread, stop = _start_runner(control_plane, "node-f", state)
    try:
        time.sleep(0.2)
        task = client.submit_task(
            [sys.executable, "-c", "import sys; sys.exit(7)"], timeout=30)[
            "task"]
        done = _wait_task(control_plane, task["task_id"], ("done", "failed"))
        assert done["status"] == "failed"
        assert done["evidence"]["exit_code"] == 7
    finally:
        stop.set()
        thread.join(timeout=5)


def test_cordon_blocks_submit(control_plane):
    state = control_plane["state_dir"]
    key = fabric_runner.load_or_create_key(str(state), "node-c")
    client = _register(control_plane, key, "node-c")
    assert client.cordon()["ok"] is True
    submitted = client.submit_task([sys.executable, "-c", "print(1)"])
    assert submitted["task"] is None
    assert "cordon" in submitted["error"]
    # undo 后恢复
    assert client.cordon(undo=True)["ok"] is True
    submitted = client.submit_task([sys.executable, "-c", "print(1)"])
    assert submitted["task"] is not None


def test_migrate_invalidates_old_fencing_token(control_plane):
    """迁移 = 新设备 + 新租约；旧 token 的执行结果无法提交（防 split-brain）。"""
    state = control_plane["state_dir"]
    key_a = fabric_runner.load_or_create_key(str(state), "node-a")
    client_a = _register(control_plane, key_a, "node-a")
    key_b = fabric_runner.load_or_create_key(str(state), "node-b")
    client_b = _register(control_plane, key_b, "node-b")
    thread_b, stop_b = _start_runner(control_plane, "node-b", state)
    try:
        time.sleep(0.2)
        task = client_a.submit_task(
            [sys.executable, "-c", "print('migrated')"], timeout=30)[
            "task"]
        old_lease = control_plane["httpd"].control.task_leases[task["task_id"]]
        # 迁移到 node-b
        result = client_a.task_migrate(task["task_id"], key_b["device_id"])
        assert result["ok"] is True
        # 旧 token 提交 → 拒收
        stale = client_a.evidence(task["task_id"], old_lease, 1,
                                  {"result": "passed"}, task_id=task["task_id"])
        assert stale["accepted"] is False
        assert "stale" in stale["reason"]
        # B 正常执行完成
        done = _wait_task(control_plane, task["task_id"], ("done", "failed"))
        assert done["status"] == "done"
    finally:
        stop_b.set()
        thread_b.join(timeout=5)


def test_stale_device_task_auto_rescheduled(control_plane):
    """设备从未心跳（失联）→ 其 stateless 任务自动重排队 → 其他 Runner 认领。"""
    state = control_plane["state_dir"]
    key_a = fabric_runner.load_or_create_key(str(state), "node-ghost")
    client_a = _register(control_plane, key_a, "node-ghost")
    key_b = fabric_runner.load_or_create_key(str(state), "node-alive")
    client_b = _register(control_plane, key_b, "node-alive")
    # 先提交到失联设备（从不心跳）
    task = client_a.submit_task(
        [sys.executable, "-c", "print('rescheduled')"], timeout=30)[
        "task"]
    # B 的 Runner 轮询触发 _reschedule_stale → 认领并执行
    thread_b, stop_b = _start_runner(control_plane, "node-alive", state)
    try:
        done = _wait_task(control_plane, task["task_id"], ("done", "failed"))
        assert done["status"] == "done"
        assert done["device_id"] == key_b["device_id"]
        assert "rescheduled" in done["evidence"]["stdout"]
    finally:
        stop_b.set()
        thread_b.join(timeout=5)


@pytest.mark.property
def test_cancel_prevents_execution(control_plane):
    state = control_plane["state_dir"]
    key = fabric_runner.load_or_create_key(str(state), "node-cx")
    client = _register(control_plane, key, "node-cx")
    thread, stop = _start_runner(control_plane, "node-cx", state)
    try:
        time.sleep(0.2)
        task = client.submit_task([sys.executable, "-c", "print(1)"])["task"]
        result = client.task_cancel(task["task_id"])
        assert result["ok"] is True
        time.sleep(0.3)  # 给 Runner 几轮轮询机会（不应执行）
        record = control_plane["httpd"].control.tasks[task["task_id"]]
        assert record["status"] == "cancelled"
        assert record["evidence"] is None
        # 已完成的不能再取消
        task2 = client.submit_task([sys.executable, "-c", "print(2)"])["task"]
        done = _wait_task(control_plane, task2["task_id"], ("done", "failed"))
        assert done["status"] == "done"
        assert client.task_cancel(task2["task_id"])["ok"] is False
    finally:
        stop.set()
        thread.join(timeout=5)


def test_cli_schedule_end_to_end(control_plane, tmp_path):
    """CLI schedule：--device 指定 → 控制平面 → Runner 执行 → task 查询。"""
    state = control_plane["state_dir"]
    thread, stop = _start_runner(control_plane, "node-cli", state)
    try:
        time.sleep(0.2)
        with pytest.raises(SystemExit) as exc:
            fabric_cli.devices_main([
                "schedule", "--device", "node-cli",
                "--control", _base(control_plane),
                "--state-dir", str(state), "--json",
                "--", sys.executable, "-c", "print('cli-ok')"])
        assert exc.value.code == 0
        # 找到刚提交的任务（最后一个 queued 任务）
        tasks = [t for t in control_plane["httpd"].control.tasks.values()
                 if " ".join(t.get("argv", [])).endswith("print('cli-ok')")]
        assert tasks
        task_id = tasks[0]["task_id"]
        done = _wait_task(control_plane, task_id, ("done", "failed"))
        assert done["status"] == "done"
        assert done["evidence"]["stdout"].strip() == "cli-ok"
    finally:
        stop.set()
        thread.join(timeout=5)


def test_schedule_auto_pick_device(control_plane):
    """无 --device：按约束自动选设备（跳过 cordoned，选负载最低）。"""
    state = control_plane["state_dir"]
    key = fabric_runner.load_or_create_key(str(state), "node-pick")
    client = _register(control_plane, key, "node-pick")
    thread, stop = _start_runner(control_plane, "node-pick", state)
    try:
        time.sleep(0.2)
        with pytest.raises(SystemExit) as exc:
            fabric_cli.devices_main([
                "schedule", "--os", "linux", "--as-device", "node-pick",
                "--control", _base(control_plane),
                "--state-dir", str(state),
                "--", sys.executable, "-c", "print(1)"])
        assert exc.value.code == 0
        tasks = [t for t in control_plane["httpd"].control.tasks.values()]
        assert tasks and tasks[-1]["device_id"] == key["device_id"]
        # 无可行设备时拒绝
        with pytest.raises(SystemExit) as exc2:
            fabric_cli.devices_main([
                "schedule", "--os", "windows", "--as-device", "node-pick",
                "--control", _base(control_plane),
                "--state-dir", str(state),
                "--", "echo", "x"])
        assert exc2.value.code == 2
    finally:
        stop.set()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# per-device 并发上限 / 事件 attempt / 时钟偏差
# ---------------------------------------------------------------------------

def test_device_quota_mixin_direct():
    """device_quota：进行中超限拒绝，完成后释放（直接调用 mixin）。"""
    from pbatch.fabric_server import ControlPlane
    plane = ControlPlane(state_dir="", auto_approve=True,
                         device_quotas={"node-x": 1})
    plane.devices["rn-abc"] = {"name": "node-x"}
    assert plane._device_quota_check("rn-abc") == ""
    plane._device_quota_inc("rn-abc")
    error = plane._device_quota_check("rn-abc")
    assert "device quota exhausted" in error
    plane._device_quota_dec("rn-abc")
    assert plane._device_quota_check("rn-abc") == ""
    # 未配置配额设备不受限
    assert plane._device_quota_check("rn-free") == ""


def test_clock_skew_report(tmp_path):
    """时钟纪律：last_seen 偏差分类（<300s online / 更久 stale）。"""
    import time as _time
    from pbatch import fabric_cli
    nodes = [
        {"name": "fresh", "dynamic_state": {
            "last_seen_at": _time.strftime("%Y-%m-%dT%H:%M:%S")}},
        {"name": "old", "dynamic_state": {
            "last_seen_at": _time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                _time.localtime(_time.time() - 3600))}},
        {"name": "never", "dynamic_state": {"last_seen_at": ""}},
    ]
    report = fabric_cli._clock_skew_report(nodes)
    by_name = {entry["name"]: entry for entry in report}
    assert by_name["fresh"]["status"] == "online"
    assert by_name["old"]["status"] == "stale"
    assert by_name["never"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# campaign 实战内化：模块发现排除打包产物（egg-info 曾被当模块分析）
# ---------------------------------------------------------------------------

def test_campaign_module_discovery_excludes_artifacts(tmp_path):
    """egg-info/__pycache__/dist 不是分析目标（campaign 实战教训）。"""
    from pbatch.campaign_analysis import discover_modules
    from pbatch.campaign import CampaignSettings
    # 构造：真实模块 + 打包产物目录
    (tmp_path / "pbatch").mkdir()
    (tmp_path / "pbatch" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "ai_batch_runner.egg-info").mkdir()
    (tmp_path / "ai_batch_runner.egg-info" / "PKG-INFO").write_text(
        "Metadata-Version: 2.1\n", encoding="utf-8")
    (tmp_path / "pbatch" / "__pycache__").mkdir()
    (tmp_path / "pbatch" / "__pycache__" / "core.cpython-310.pyc").write_text(
        "x", encoding="utf-8")
    settings = CampaignSettings(
        roots=("",), max_depth=2, excludes=("docs",),
        fallback_top_level=True)
    modules = discover_modules(tmp_path, settings)
    assert "pbatch" in modules
    assert not any("egg-info" in m for m in modules)
    assert not any("__pycache__" in m for m in modules)


def test_campaign_excludes_package_artifact_dirs():
    from pathlib import Path
    from pbatch.campaign_analysis import _excluded
    assert _excluded(Path("ai_batch_runner.egg-info"), ()) is True
    assert _excluded(Path("pbatch/__pycache__"), ()) is True
    assert _excluded(Path("dist"), ()) is True
    assert _excluded(Path("pbatch"), ()) is False
