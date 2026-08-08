"""Tests for 交互事件账本 / 设备预算上限 / 投机执行.

events: 控制平面追加式事件（submit/evidence/migrate/halt/revoke）、
有界读取、CLI recent。
budget: 设备累计预算预留、超限拒绝、无预算时不限。
speculative: 同任务双设备竞跑、先完成者胜、败者取消（at-least-once）。
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


def _client(control_plane, name):
    key = fabric_runner.load_or_create_key(
        str(control_plane["state_dir"]), name)
    client = RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                       key["secret"], key["device_id"])
    assert client.register(name, fabric.local_probe())["approved"] is True
    return client, key


# ---------------------------------------------------------------------------
# 交互事件账本
# ---------------------------------------------------------------------------

def test_control_plane_records_interaction_events(control_plane):
    client, _ = _client(control_plane, "node-ev")
    task = client.submit_task(["echo", "x"])["task"]
    client.task_cancel(task["task_id"])
    client.halt()
    from pbatch.events import read_events
    records = read_events(str(control_plane["state_dir"]))
    verbs = [r["verb"] for r in records]
    assert "register" in verbs
    assert "submit" in verbs
    assert "halt" in verbs
    submit = next(r for r in records if r["verb"] == "submit")
    assert submit["object_id"] == task["task_id"]
    assert submit["source"] == "control_plane"


def test_events_append_only_bounded(control_plane):
    """事件账本只追加；CLI 读取有界（新→旧）。"""
    client, _ = _client(control_plane, "node-ev2")
    for index in range(5):
        client.submit_task([f"echo {index}"])
    from pbatch.events import read_events
    records = read_events(str(control_plane["state_dir"]), limit=3)
    assert len(records) <= 3
    assert records[0]["verb"] == "submit"  # 最新在前
    assert records[0]["object_id"] != records[-1]["object_id"]


def test_events_cli(control_plane, capsys):
    from pbatch import events
    client, _ = _client(control_plane, "node-ev3")
    client.submit_task(["echo", "x"])
    with pytest.raises(SystemExit) as exc:
        events.events_main(["--state-dir", str(control_plane["state_dir"]),
                            "--json", "--limit", "2"])
    assert exc.value.code == 0
    records = json.loads(capsys.readouterr().out)
    assert records and records[0]["verb"] == "submit"


# ---------------------------------------------------------------------------
# 设备侧预算上限（§41.4）
# ---------------------------------------------------------------------------

def test_device_budget_cap_reserves_and_rejects(tmp_path):
    """提交即预留：累计超过 cap → 拒绝（防失控任务烧配额）。"""
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True, budget_cap=5.0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        key = fabric_runner.load_or_create_key(str(tmp_path / "state"),
                                               "node-bd")
        client = RpcClient(f"http://127.0.0.1:{port}", key["secret"],
                           key["device_id"])
        assert client.register("node-bd", fabric.local_probe())[
            "approved"] is True
        ok = client.submit_task(["echo", "1"], estimated_cost=3.0)
        assert ok["task"] is not None
        rejected = client.submit_task(["echo", "2"], estimated_cost=3.0)
        assert rejected["task"] is None
        assert "budget exhausted" in rejected["error"]
        ok2 = client.submit_task(["echo", "3"], estimated_cost=2.0)
        assert ok2["task"] is not None  # 3+2 = 5 ≤ cap
        rejected2 = client.submit_task(["echo", "4"], estimated_cost=0.1)
        assert rejected2["task"] is None
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_no_budget_cap_unlimited(control_plane):
    client, _ = _client(control_plane, "node-bd2")
    for index in range(10):
        ok = client.submit_task([f"echo {index}"], estimated_cost=1.0)
        assert ok["task"] is not None


# ---------------------------------------------------------------------------
# 投机执行（§33）
# ---------------------------------------------------------------------------

def test_speculative_race_first_wins(control_plane, monkeypatch):
    """同任务双设备竞跑：至少一个完成，无失败任务（at-least-once）。"""
    state = control_plane["state_dir"]
    keys = {}
    for name in ("node-ra", "node-rb"):
        key = fabric_runner.load_or_create_key(str(state), name)
        keys[name] = key
        client = RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                           key["secret"], key["device_id"])
        assert client.register(name, fabric.local_probe())["approved"] is True
    stops = []
    threads = []
    for name in ("node-ra", "node-rb"):
        stop = threading.Event()
        stops.append(stop)
        thread = threading.Thread(
            target=fabric_runner.run_runner_loop,
            args=(f"http://127.0.0.1:{control_plane['port']}", name,
                  str(state)),
            kwargs={"heartbeat_interval": 0.03, "stop_event": stop},
            daemon=True)
        thread.start()
        threads.append(thread)
    try:
        time.sleep(0.2)
        from pbatch import fabric_cmds
        import argparse
        args = argparse.Namespace(
            cmd=["python3", "-c", "print('race-ok')"], cwd="", timeout=30,
            effect_class="read_only", mobility="stateless",
            checkpoint_save="", checkpoint_load="", estimated_cost=0.0,
            speculative=True, devices="node-ra,node-rb",
            json=True, control=f"http://127.0.0.1:{control_plane['port']}",
            state_dir=str(state), device="node-ra", as_device="",
            os=[], arch=[], min_memory_mb=0, min_cpu=0,
            cluster="", gang=False, region="")
        # 直接调用（跳过 argparse）
        key = keys["node-ra"]
        client = RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                           key["secret"], key["device_id"])
        monkeypatch.setattr(fabric_cmds, "_control_client",
                            lambda a, acting="node-ra": client)
        with pytest.raises(SystemExit) as exc:
            fabric_cmds._cmd_schedule(args)
        assert exc.value.code == 0
        # 竞跑结果：胜者存在
        tasks = [t for t in control_plane["httpd"].control.tasks.values()
                 if "race-ok" in " ".join(t.get("argv", []))]
        assert tasks
        statuses = [t["status"] for t in tasks]
        assert "done" in statuses
        assert "failed" not in statuses
    finally:
        for stop in stops:
            stop.set()
        for thread in threads:
            thread.join(timeout=5)
