"""Tests for P8 类型化超图 + 设备织网收尾（Kill Switch / 失联演练 / 传输计量）.

hypergraph: 节点/超边构建、结构校验、guards 多条件求值、依赖环检测、
冲突显式暴露、级联失效、JSON 往返。
fabric: /halt 冻结全部排队任务 + 全设备 cordon；runner 失联演练
（drill-skip-heartbeats → 控制平面重排队任务）；scp 传输字节计量。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from pbatch import fabric, fabric_exec, fabric_runner, fabric_server
from pbatch.fabric_rpc import RpcClient
from pbatch.hypergraph import (DIRECTED, NODE_TYPES, RELATION_TYPES,
                               Hypergraph)


# ---------------------------------------------------------------------------
# P8: 类型化超图
# ---------------------------------------------------------------------------

def _approval_graph() -> Hypergraph:
    """文件审批超图：多条件共同约束一个状态变化（超图语义）。"""
    graph = Hypergraph()
    for node_id, node_type in (("user-perm", "atom"), ("file-pending", "atom"),
                               ("sheet-free", "atom"), ("opinion-ok", "atom"),
                               ("approve", "transaction"),
                               ("audit", "transaction"),
                               ("notify", "transaction")):
        graph.add_node(node_id, node_type)
    graph.add_relation(["user-perm", "file-pending", "sheet-free",
                        "opinion-ok"], ["approve"], "guards",
                       condition="权限+状态+占用+意见 同时成立")
    graph.add_relation(["approve"], ["audit"], "depends_on")
    graph.add_relation(["approve"], ["notify"], "depends_on")
    graph.add_relation(["notify"], ["audit"], "compensates")
    graph.add_node("sync-export", "transaction")
    graph.add_node("async-export", "transaction")
    graph.add_relation(["sync-export"], ["async-export"], "alternative_to")
    return graph


def test_hypergraph_build_and_types():
    graph = _approval_graph()
    assert len(graph.nodes) == 9  # 7 个主体 + sync/async-export
    assert len(graph.relations) == 5
    assert set(NODE_TYPES) >= {"atom", "transaction", "capability"}
    assert "depends_on" in RELATION_TYPES and "guards" in RELATION_TYPES
    assert "depends_on" in DIRECTED


def test_hypergraph_validate_ok():
    assert _approval_graph().validate() == []


def test_hypergraph_validate_unknown_endpoint():
    graph = Hypergraph()
    graph.add_node("a", "atom")
    graph.add_relation(["a"], ["ghost"], "depends_on")
    violations = graph.validate()
    assert any("unknown endpoint" in v for v in violations)


def test_hypergraph_validate_self_loop_and_bad_type():
    graph = Hypergraph()
    graph.add_node("a", "atom")
    graph.add_relation(["a"], ["a"], "depends_on")
    violations = graph.validate()
    assert any("self-loop" in v for v in violations)
    with pytest.raises(ValueError):  # 非法类型在构建期 fail fast
        graph.add_relation(["a"], ["a"], "teleports")
    with pytest.raises(ValueError):
        graph.add_node("b", "teleporter")


def test_guards_require_all_conditions():
    """超图语义：多个条件共同约束——缺一个 guard 前置就不成立。"""
    graph = _approval_graph()
    satisfied = {"user-perm", "file-pending", "sheet-free"}
    assert graph.guards_satisfied("approve", satisfied) is False
    satisfied.add("opinion-ok")
    assert graph.guards_satisfied("approve", satisfied) is True
    assert graph.guards_satisfied("audit", set()) is True  # 无 guards → 成立


def test_dependencies_hyperedge_multi_input():
    graph = _approval_graph()
    deps = graph.dependencies("approve")
    assert deps == []  # approve 由 guards 约束而非 depends_on
    audit_deps = graph.dependencies("audit")
    assert any(dep["from"] == ["approve"] for dep in audit_deps)


def test_cycle_detection():
    graph = Hypergraph()
    for node_id in ("a", "b", "c"):
        graph.add_node(node_id, "transaction")
    graph.add_relation(["a"], ["b"], "depends_on")
    graph.add_relation(["b"], ["c"], "depends_on")
    graph.add_relation(["c"], ["a"], "depends_on")
    cycles = graph.depends_on_cycles()
    assert cycles, "依赖环必须被检测（死锁预防）"


def test_no_cycles_in_approval_graph():
    assert _approval_graph().depends_on_cycles() == []


def test_conflicts_explicit():
    graph = _approval_graph()
    conflicts = graph.conflicts()
    assert len(conflicts) == 0
    graph.add_relation(["notify"], ["audit"], "conflicts_with",
                       condition="通知失败时审计不应标记成功")
    conflicts = graph.conflicts()
    assert conflicts and set(conflicts[0]["between"]) == {"notify", "audit"}


def test_cascade_invalidate():
    """前提失效 → 沿 depends_on 传播（真值维护在超图上的语义）。"""
    graph = _approval_graph()
    affected = graph.cascade_invalidate("approve")
    assert "audit" in affected and "notify" in affected
    assert graph.cascade_invalidate("user-perm") == ["user-perm"]


def test_json_roundtrip():
    graph = _approval_graph()
    restored = Hypergraph.from_dict(graph.to_dict())
    assert restored.validate() == []
    assert set(restored.nodes) == set(graph.nodes)
    assert len(restored.relations) == len(graph.relations)
    assert restored.guards_satisfied(
        "approve", {"user-perm", "file-pending", "sheet-free",
                    "opinion-ok"}) is True


def test_graph_cli(tmp_path, capsys):
    from pbatch.hypergraph import graph_main
    payload = _approval_graph().to_dict()
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        graph_main(["validate", "--input", str(path)])
    assert exc.value.code == 0
    capsys.readouterr()  # 清空 validate 输出
    with pytest.raises(SystemExit) as exc2:
        graph_main(["deps", "--input", str(path), "--node", "audit", "--json"])
    assert exc2.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dependencies"][0]["from"] == ["approve"]
    with pytest.raises(SystemExit) as exc3:
        graph_main(["cycles", "--input", str(path)])
    assert exc3.value.code == 0


def test_graph_cli_invalid_input(tmp_path, capsys):
    from pbatch.hypergraph import graph_main
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"nodes": [{"id": "a", "type": "atom"}],
                               "relations": [{"id": "r1", "from": ["a"],
                                              "to": ["missing"],
                                              "type": "depends_on"}]}),
                   encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        graph_main(["validate", "--input", str(bad)])
    assert exc.value.code == 1
    assert "unknown endpoint" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Kill Switch（/halt）
# ---------------------------------------------------------------------------

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


def _registered_client(control_plane, name):
    key = fabric_runner.load_or_create_key(str(control_plane["state_dir"]), name)
    client = RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                       key["secret"], key["device_id"])
    assert client.register(name, fabric.local_probe())["approved"] is True
    return client


def _wait_for_heartbeats(control_plane, device_id, count: int,
                        deadline_seconds: float = 10.0) -> int:
    """Wait until the control plane has counted `count` heartbeats.

    Startup is excluded on purpose: the runner's first cycle still pays the
    one-time full capability probe (an `nvidia-smi` fork), which says nothing
    about the steady-state heartbeat rate under test.
    """
    deadline = time.monotonic() + deadline_seconds
    seen = 0
    while time.monotonic() < deadline:
        device = control_plane["httpd"].control.devices.get(device_id, {})
        seen = int(device.get("heartbeats", 0) or 0)
        if seen >= count:
            return seen
        time.sleep(0.01)
    return seen


@pytest.mark.chaos
def test_halt_cancels_all_and_cordons(control_plane):
    """Kill Switch：排队任务全部取消 + 所有设备 cordon。"""
    client_a = _registered_client(control_plane, "node-a")
    client_b = _registered_client(control_plane, "node-b")
    task = client_a.submit_task(["echo", "x"])["task"]
    assert task["status"] == "queued"
    result = client_a.halt()
    assert result["ok"] is True
    assert result["cancelled_tasks"] >= 1
    assert result["cordoned_devices"] >= 2
    record = control_plane["httpd"].control.tasks[task["task_id"]]
    assert record["status"] == "cancelled"
    assert record.get("cancelled_by") == "halt"
    # 全设备 cordon：新提交被拒
    submitted = client_b.submit_task(["echo", "y"])
    assert submitted["task"] is None
    assert "cordon" in submitted["error"]


def test_devices_halt_cli(control_plane, capsys):
    from pbatch import fabric_cli
    state = control_plane["state_dir"]
    key = fabric_runner.load_or_create_key(str(state), "node-h")
    RpcClient(f"http://127.0.0.1:{control_plane['port']}", key["secret"],
              key["device_id"]).register("node-h", fabric.local_probe())
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["halt", "--device", "node-h",
                                 "--control",
                                 f"http://127.0.0.1:{control_plane['port']}",
                                 "--state-dir", str(state)])
    assert exc.value.code == 0
    assert "Kill Switch" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 失联演练（drill-skip-heartbeats → 断线对账）
# ---------------------------------------------------------------------------

@pytest.mark.chaos
def test_drill_skip_heartbeats_triggers_reschedule(control_plane, monkeypatch):
    """演练：runner 注册后跳过心跳 → 控制平面标 stale → 任务重排队到
    另一个 runner（断线对账路径真实可用）。"""
    monkeypatch.setattr(fabric_server, "HEARTBEAT_STALE_SECONDS", 0.2)
    state = control_plane["state_dir"]
    ghost = _registered_client(control_plane, "node-ghost")
    alive_key = fabric_runner.load_or_create_key(str(state), "node-alive")
    alive = RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                      alive_key["secret"], alive_key["device_id"])
    assert alive.register("node-alive", fabric.local_probe())["approved"] is True

    # 失联设备上提交任务
    task = ghost.submit_task(["python3", "-c", "print('drilled')"])["task"]
    # 正常 runner 循环（短心跳）会触发 _reschedule_stale 并认领
    stop = threading.Event()
    thread = threading.Thread(
        target=fabric_runner.run_runner_loop,
        args=(f"http://127.0.0.1:{control_plane['port']}", "node-alive",
              str(state)),
        kwargs={"heartbeat_interval": 0.05, "stop_event": stop}, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            record = control_plane["httpd"].control.tasks.get(task["task_id"], {})
            if record.get("status") == "done":
                break
            time.sleep(0.02)
        assert record["status"] == "done"
        assert record["device_id"] == alive_key["device_id"]
        assert "drilled" in record["evidence"]["stdout"]
    finally:
        stop.set()
        thread.join(timeout=5)


def test_drill_skip_zero_normal_heartbeats(control_plane, monkeypatch):
    """drill_skip_heartbeats=0：正常心跳（不进入演练分支）。"""
    state = control_plane["state_dir"]
    key = fabric_runner.load_or_create_key(str(state), "node-normal")
    client = RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                       key["secret"], key["device_id"])
    assert client.register("node-normal", fabric.local_probe())["approved"] is True
    stop = threading.Event()
    thread = threading.Thread(
        target=fabric_runner.run_runner_loop,
        args=(f"http://127.0.0.1:{control_plane['port']}", "node-normal",
              str(state)),
        kwargs={"heartbeat_interval": 0.02, "stop_event": stop}, daemon=True)
    thread.start()
    # Registration's one-time capability probe is startup, not heartbeat rate.
    _wait_for_heartbeats(control_plane, key["device_id"], 1)
    observed = _wait_for_heartbeats(control_plane, key["device_id"], 4)
    stop.set()
    thread.join(timeout=5)
    assert observed >= 4, f"heartbeat rate collapsed: only {observed} recorded"


# ---------------------------------------------------------------------------
# 传输计量（AADM-D §41.8）
# ---------------------------------------------------------------------------

def test_transfer_file_bytes_measured(monkeypatch):
    """上传计量传输字节（metered 网络下传输量是成本）。"""
    from pathlib import Path as _Path
    local = _Path("/tmp/fabric-transfer-test.txt")
    local.write_text("0123456789", encoding="utf-8")

    def fake_spawn(spec, env, on_spawn=None):
        fake_proc = type("P", (), {"returncode": 0})()
        return (fake_proc, "", "", False, False)

    monkeypatch.setattr(fabric_exec, "_spawn_bounded", fake_spawn)
    result = fabric_exec._transfer_file(
        {"name": "n", "host_name": "n"}, str(local), "/remote/f")
    assert result["ok"] is True
    assert result["bytes"] == 10
    local.unlink()


def test_download_transfer_bytes_measured(monkeypatch, tmp_path):
    """下载也纳入网络计量，而不是只统计上传。"""
    local = tmp_path / "download.txt"
    local.write_text("download", encoding="utf-8")

    def fake_spawn(spec, env, on_spawn=None):
        fake_proc = type("P", (), {"returncode": 0})()
        return (fake_proc, "", "", False, False)

    monkeypatch.setattr(fabric_exec, "_spawn_bounded", fake_spawn)
    result = fabric_exec._transfer_file(
        {"name": "n", "host_name": "n"}, str(local), "/remote/f",
        download=True)
    assert result["ok"] is True
    assert result["bytes"] == 8


def test_graph_ops_seven_operations():
    """AADM-R §8：动态图七操作映射完整。"""
    from pbatch.hypergraph import GRAPH_OPS
    ops = [op for op, _, _ in GRAPH_OPS]
    assert ops == ["expand", "collapse", "replace", "prune",
                   "insert", "reroute", "merge"]
    for _, meaning, implementation in GRAPH_OPS:
        assert meaning and implementation


def test_graph_ops_cli_json(capsys):
    import pytest as _pytest
    from pbatch.hypergraph import graph_main
    with _pytest.raises(SystemExit) as exc:
        graph_main(["ops", "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report) == 7
    assert report[0]["op"] == "expand"
