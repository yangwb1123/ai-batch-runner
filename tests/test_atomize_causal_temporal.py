"""Tests for 原子化 / 因果假设 / 时间衰减 / Checkpoint（设备织网 Phase 5）.

atomize: 确定性原子提取（目标/角色/对象/行为/约束硬度/未知/风险/验收）、
超图导出。
causality: 假设生命周期（unverified→supported/rejected）、证据更新、
日志回放。
temporal: 置信度指数衰减（volatile/slow_changing/static）、新鲜度判定。
checkpoint: 控制平面存取、Runner 执行前恢复/执行后回传（断点续传）、
迁移后新设备取到检查点。
"""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

import pytest

from pbatch import (atomize, causality, fabric, fabric_runner, fabric_server,
                    temporal)
from pbatch.fabric_rpc import RpcClient


# ---------------------------------------------------------------------------
# atomize
# ---------------------------------------------------------------------------

def test_atomize_extracts_types_and_hardness():
    report = atomize.atomize(
        "审批人可以拒绝文件申请，审批完成后必须记录审计")
    by_type = {}
    for atom in report["atoms"]:
        by_type.setdefault(atom["type"], []).append(atom)
    assert "actor" in by_type and "operation" in by_type
    assert "object" in by_type
    # 硬约束：必须 → hardness=hard
    constraint = by_type["constraint"][0]
    assert constraint["hardness"] == "hard"
    assert constraint["source"] == "text"  # 来源必带（推断≠事实）
    assert 0 <= constraint["confidence"] <= 1


def test_atomize_unknowns_and_acceptance():
    report = atomize.atomize("是否支持多级审批？验收标准是并发测试通过")
    types = {atom["type"] for atom in report["atoms"]}
    assert "unknown" in types
    assert "acceptance" in types


def test_atomize_empty():
    assert atomize.atomize("")["count"] == 0


def test_atoms_to_graph_valid():
    report = atomize.atomize("审批必须记录审计")
    graph = atomize.atoms_to_graph(report["atoms"])
    assert graph["nodes"]
    # 硬约束 → guards 关系指向 constraint-root
    assert any(node["id"] == "constraint-root" for node in graph["nodes"])
    assert any(r["type"] == "guards" for r in graph["relations"])


def test_atomize_cli_json(capsys):
    with pytest.raises(SystemExit) as exc:
        atomize.atomize_main(["审批人可以拒绝文件申请", "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["count"] > 0


# ---------------------------------------------------------------------------
# causality
# ---------------------------------------------------------------------------

def test_causal_lifecycle():
    hypothesis = causality.new_hypothesis(
        "接口慢", "缓存缺失", "加缓存", "P95 下降 50%")
    assert hypothesis["status"] == "unverified"
    assert hypothesis["confidence"] == 0.3  # 未证实因果只有低置信度
    causality.apply_evidence(hypothesis, supports="命中率 90%")
    assert hypothesis["confidence"] > 0.5
    assert hypothesis["status"] == "supported"
    # 反驳实验加入 → 双向证据 → 未定论（honest：不被支持证据绑架）
    causality.apply_evidence(hypothesis, refutes="P95 无变化")
    assert hypothesis["status"] == "inconclusive"
    assert hypothesis["confidence"] < 0.5
    # 只有反驳、无支持 → 拒绝
    only_refutes = causality.new_hypothesis("慢", "原因 B")
    causality.apply_evidence(only_refutes, refutes="实验证明无关")
    assert only_refutes["status"] == "rejected"


def test_causal_inconclusive_when_mixed():
    hypothesis = causality.new_hypothesis("卡顿", "DOM 过多")
    causality.apply_evidence(hypothesis, supports="DOM 1 万", refutes="虚拟滚动后仍卡")
    assert hypothesis["status"] == "inconclusive"


def test_causal_log_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(causality, "CAUSALITY_FILE", tmp_path / "c.jsonl")
    causality._append({"action": "new", "payload": {
        "id": "H-1", "symptom": "接口慢", "cause": "缓存缺失",
        "intervention": "加缓存", "expected": "P95 降"}})
    causality._append({"action": "evidence", "payload": {
        "id": "H-1", "supports": "命中率高", "refutes": "", "measurement": ""}})
    store = causality.load_hypotheses(tmp_path / "c.jsonl")
    assert store["H-1"]["status"] == "supported"


# ---------------------------------------------------------------------------
# temporal
# ---------------------------------------------------------------------------

def test_decay_volatile():
    assert temporal.decayed_confidence(0.9, "volatile", 0) == 0.9
    decayed = temporal.decayed_confidence(0.9, "volatile", 12 * 3600)
    assert decayed == pytest.approx(0.9 * 0.25, abs=0.001)  # 两个半衰期


def test_decay_slow_changing():
    decayed = temporal.decayed_confidence(0.8, "slow_changing", 7 * 86400)
    assert decayed == pytest.approx(0.4, abs=0.001)  # 一个半衰期


def test_decay_static_never_decays():
    assert temporal.decayed_confidence(0.9, "static", 10 ** 9) == 0.9


def test_decay_invalid_input():
    with pytest.raises(ValueError):
        temporal.decayed_confidence(1.5, "volatile", 1)
    with pytest.raises(ValueError):
        temporal.decayed_confidence(0.5, "bogus", 1)


def test_is_fresh_threshold():
    assert temporal.is_fresh(0.6, threshold=0.5) is True
    assert temporal.is_fresh(0.4, threshold=0.5) is False


# ---------------------------------------------------------------------------
# checkpoint（断点续传跨设备）
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


def _client(control_plane, name):
    key = fabric_runner.load_or_create_key(
        str(control_plane["state_dir"]), name)
    client = RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                       key["secret"], key["device_id"])
    assert client.register(name, fabric.local_probe())["approved"] is True
    return client


def test_checkpoint_upload_download_roundtrip(control_plane):
    client = _client(control_plane, "node-cp")
    task = client.submit_task(["echo", "checkpoint"],
                              mobility="checkpointable")["task"]
    lease = task["lease"]
    blob = base64.b64encode(b"progress=42").decode("ascii")
    uploaded = client.checkpoint_upload(task["task_id"], blob,
                                        lease["lease_id"],
                                        lease["fencing_token"])
    assert uploaded["ok"] is True
    assert uploaded["digest"]
    downloaded = client.checkpoint_download(task["task_id"],
                                            lease["lease_id"],
                                            lease["fencing_token"])
    assert downloaded["has_checkpoint"] is True
    assert base64.b64decode(downloaded["blob_b64"]) == b"progress=42"
    assert client.checkpoint_download("t-none")["has_checkpoint"] is False
    outsider = _client(control_plane, "node-cp-outsider")
    denied = outsider.checkpoint_download(task["task_id"],
                                          lease["lease_id"],
                                          lease["fencing_token"])
    assert denied["has_checkpoint"] is False
    assert "lease" in denied["error"]


def test_checkpoint_submit_with_task(control_plane):
    """schedule 携带检查点 → 任务记录里可查（迁移后新设备续传）。"""
    client = _client(control_plane, "node-cp2")
    blob = base64.b64encode(b"state-1").decode("ascii")
    task = client.submit_task(["echo", "x"], mobility="checkpointable",
                              checkpoint_blob=blob,
                              checkpoint_remote="state.json")["task"]
    stored = control_plane["httpd"].control.checkpoints.get(task["task_id"])
    assert stored and stored["digest"]


def test_runner_restores_and_saves_checkpoint(tmp_path, control_plane):
    """Runner 执行前恢复检查点到 cwd，执行后回传更新。"""
    state = control_plane["state_dir"]
    workdir = tmp_path / "taskwork"
    workdir.mkdir()
    client = _client(control_plane, "node-cp3")
    blob = base64.b64encode(b"epoch=1").decode("ascii")
    task = client.submit_task(
        ["python3", "-c",
         "open('state.json','w').write('epoch=2')"],
        cwd=str(workdir), mobility="checkpointable",
        checkpoint_blob=blob, checkpoint_remote="state.json")["task"]
    # 模拟 Runner 的恢复+执行+回传
    fabric_runner._restore_checkpoint(client, task)
    assert (workdir / "state.json").read_text() == "epoch=1"  # 断点续传起点
    spec = fabric.ExecutionSpec(argv=["python3", "-c",
                                      "open('state.json','w').write('epoch=2')"],
                                cwd=str(workdir), timeout=30)
    fabric.LocalExecutionTarget().execute(spec)
    fabric_runner._save_checkpoint(client, task, str(workdir))
    stored = control_plane["httpd"].control.checkpoints[task["task_id"]]
    assert base64.b64decode(stored["blob_b64"]) == b"epoch=2"  # 回传更新


def test_checkpoint_oversize_rejected(control_plane):
    client = _client(control_plane, "node-cp4")
    blob = "A" * (fabric_server.CHECKPOINT_MAX_B64 + 10)
    result = client.checkpoint_upload("t-big", blob)
    assert result["ok"] is False
