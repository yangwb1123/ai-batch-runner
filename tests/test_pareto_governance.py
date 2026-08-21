"""Tests for Pareto/MUC、runtime_policy、估计区间、GoalModel、should_fork、
设备退役、checkpoint 校验、回滚决策、错误预算。

pareto: 非支配前沿、Utility 选择、MUC（冲突约束显式暴露）。
runtime_policy: 声明式配置解析 + profile 注入（start_mode 下界）。
estimates/goal_model: p50/p90 区间、护栏指标分离。
should_fork: 分叉判据（高不确定性开路径，明确需求不开）。
revoke: 设备退役吊销密钥、任务标 lost、重注册拒绝。
checkpoint: digest 校验失败拒绝恢复。
recovery/budget: 回滚vs前向修复决策、错误预算账本。
"""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

import pytest

from pbatch import (config, fabric, fabric_runner, fabric_server, pareto,
                    profile, racing, recovery)
from pbatch.fabric_rpc import RpcClient


# ---------------------------------------------------------------------------
# pareto：前沿 / Utility / MUC
# ---------------------------------------------------------------------------

def test_pareto_frontier_filters_dominated():
    candidates = [
        {"id": "minimal", "scores": {"value": 0.5, "risk": 0.2}},
        {"id": "balanced", "scores": {"value": 0.8, "risk": 0.5}},
        {"id": "fancy", "scores": {"value": 0.9, "risk": 0.8}},
        {"id": "dominated", "scores": {"value": 0.4, "risk": 0.8}},
    ]
    frontier = pareto.pareto_frontier(candidates, ("value", "risk"),
                                      minimize={"risk"})
    ids = {c["id"] for c in frontier}
    assert "dominated" not in ids  # 被 minimal 全面压制
    assert {"minimal", "balanced", "fancy"} <= ids


def test_select_by_utility_weights():
    frontier = [
        {"id": "fast", "scores": {"value": 0.6, "risk": 0.1}},
        {"id": "solid", "scores": {"value": 0.9, "risk": 0.6}},
    ]
    # 高风险任务：风险权重高 → 选 fast
    chosen = pareto.select_by_utility(frontier, {"value": 0.4, "risk": 0.6},
                                      minimize={"risk"})
    assert chosen["id"] == "fast"
    # 平台能力任务：价值权重高 → 选 solid
    chosen2 = pareto.select_by_utility(frontier, {"value": 0.9, "risk": 0.1},
                                       minimize={"risk"})
    assert chosen2["id"] == "solid"


def test_minimal_unsatisfied_core_conflicting_requirements():
    """要求 A（不得改公共 API）+ 要求 B（必须改公共 API 结构）→ MUC=[A,B]。"""
    candidates = [
        {"id": "plan-1", "violated": ["no-public-api-change"]},
        {"id": "plan-2", "violated": ["must-change-public-api"]},
        {"id": "plan-3", "violated": ["no-public-api-change",
                                      "must-change-public-api"]},
    ]
    core = pareto.minimal_unsatisfied_core(candidates)
    assert set(core) == {"no-public-api-change", "must-change-public-api"}


def test_muc_empty_when_feasible():
    candidates = [{"id": "ok", "violated": []},
                  {"id": "bad", "violated": ["x"]}]
    assert pareto.minimal_unsatisfied_core(candidates) == []


def test_pareto_cli(tmp_path, capsys):
    from pbatch.pareto import pareto_main
    payload = {
        "objectives": ["value", "risk"], "minimize": ["risk"],
        "weights": {"value": 0.5, "risk": 0.5},
        "candidates": [
            {"id": "a", "scores": {"value": 0.9, "risk": 0.4}},
            {"id": "b", "scores": {"value": 0.5, "risk": 0.1}},
        ],
    }
    path = tmp_path / "p.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        pareto_main(["--input", str(path), "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report["frontier"]) == {"a", "b"}


# ---------------------------------------------------------------------------
# runtime_policy / estimates / goal_model / should_fork
# ---------------------------------------------------------------------------

def test_runtime_policy_config_defaults():
    from pbatch import runtime_policy
    view = runtime_policy.runtime_policy_view()
    assert view["start_mode"] == 0
    assert view["agents"]["default_count"] == 1
    assert view["resources"]["observe"] is True
    assert view["quality"]["never_skip_required_proofs"] is True
    assert view["strategies"]["max_paths"] == 3


def test_runtime_policy_start_mode_floor(monkeypatch):
    """start_mode 是模式下界：策略说 L1 起步就不降到 L0。"""
    from pbatch import runtime_policy
    report = profile.task_profile("把订单页面顶部三个按钮对齐，并统一间距")
    assert report["mode"] <= 1
    monkeypatch.setattr(runtime_policy, "RUNTIME_START_MODE", 2)
    report2 = profile.task_profile("把订单页面顶部三个按钮对齐，并统一间距")
    assert report2["mode"] == 2  # 被 start_mode 抬到下界


def test_estimates_p50_p90_shape():
    report = profile.task_profile("在 snaplink-console 增加文件审批，审批后记录审计并通知申请人")
    estimates = report["estimates"]
    assert estimates["elapsed_time"]["p50"] > 0
    assert estimates["elapsed_time"]["p90"] > estimates["elapsed_time"]["p50"]
    assert 0 < estimates["elapsed_time"]["confidence"] <= 1
    assert estimates["tokens"]["p90"] > estimates["tokens"]["p50"]


def test_goal_model_guardrails():
    model = profile.goal_model(
        "提高订单页首屏速度，不减少用户可见数据，不牺牲可访问性")
    assert model["guardrail_metrics"]  # 护栏指标与主目标分离
    assert any("不牺牲" in g for g in model["guardrail_metrics"])
    assert model["primary_goal"]


def test_should_fork_high_uncertainty():
    report = profile.task_profile(
        "评估并实现文件同步方案：本地优先 or Outbox+事件驱动，尚无先例")
    decision = racing.should_fork(report)
    assert decision["fork"] is True
    assert decision["reasons"]


def test_should_fork_no_fork_for_clear_task():
    report = profile.task_profile("把订单页面顶部三个按钮对齐，并统一间距")
    decision = racing.should_fork(report)
    assert decision["fork"] is False
    assert decision["blockers"]


# ---------------------------------------------------------------------------
# 设备退役（§41.2）+ checkpoint 校验（§41.3）
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
    return client, key


def test_revoke_device_lifecycle(control_plane):
    """退役：密钥吊销、任务标 lost、重注册被拒、重启后仍吊销。"""
    client, key = _client(control_plane, "node-retire")
    task = client.submit_task(["echo", "x"])["task"]
    result = client.revoke(reason="设备退役")
    assert result["ok"] is True
    assert result["tasks_marked_lost"] >= 1
    assert control_plane["httpd"].control.tasks[task["task_id"]]["status"] == "lost"
    # 密钥已吊销：心跳 404
    code, data = fabric_server_http_probe(client)
    assert code == 404
    # 重注册被拒（吊销是永久身份失效）
    re_register = RpcClient(f"http://127.0.0.1:{control_plane['port']}",
                            key["secret"], key["device_id"])
    assert re_register.register("node-retire", fabric.local_probe())[
        "approved"] is False
    # 重启后吊销仍然生效（revocations.jsonl 持久化）
    port = control_plane["port"]
    control_plane["httpd"].shutdown()
    control_plane["httpd"].server_close()
    httpd2, port2 = fabric_server.start_server(
        port, str(control_plane["state_dir"]))
    thread2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
    thread2.start()
    try:
        assert httpd2.control.secret_for(key["device_id"]) is None
        assert key["device_id"] in httpd2.control.revoked
    finally:
        httpd2.shutdown()
        httpd2.server_close()
        thread2.join(timeout=5)


def fabric_server_http_probe(client):
    """绕过签名发一个心跳，验证 404（密钥已吊销）。"""
    from pbatch import fabric_rpc
    return fabric_rpc.rpc_post(client.base_url, client.secret, "POST",
                               "/heartbeat",
                               {"device_id": client.device_id}, timeout=3)


def test_checkpoint_digest_mismatch_refuses_restore(tmp_path, control_plane,
                                                    monkeypatch):
    """digest 校验失败 → 拒绝恢复（防损坏/篡改，§41.3）。"""
    state = control_plane["state_dir"]
    workdir = tmp_path / "w"
    workdir.mkdir()
    client, _ = _client(control_plane, "node-cpv")
    task = client.submit_task(["echo", "x"], cwd=str(workdir),
                              mobility="checkpointable",
                              checkpoint_remote="state.json")["task"]

    def fake_download(task_id, *args, **kwargs):
        return {"has_checkpoint": True,
                "blob_b64": base64.b64encode(b"state").decode(),
                "digest": "deadbeef" * 4,  # 错误 digest
                "size": 5}

    monkeypatch.setattr(client, "checkpoint_download", fake_download)
    fabric_runner._restore_checkpoint(client, task)
    assert not (workdir / "state.json").exists()  # 校验失败 → 不写


def test_checkpoint_digest_match_restores(tmp_path, control_plane, monkeypatch):
    state = control_plane["state_dir"]
    workdir = tmp_path / "w2"
    workdir.mkdir()
    client, _ = _client(control_plane, "node-cpv2")
    task = client.submit_task(["echo", "x"], cwd=str(workdir),
                              mobility="checkpointable",
                              checkpoint_remote="state.json")["task"]
    blob = base64.b64encode(b"good-state").decode()
    import hashlib
    digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
    monkeypatch.setattr(client, "checkpoint_download",
                        lambda tid, *args, **kwargs: {
                            "has_checkpoint": True, "blob_b64": blob,
                            "digest": digest, "size": 10})
    fabric_runner._restore_checkpoint(client, task)
    assert (workdir / "state.json").read_text() == "good-state"


# ---------------------------------------------------------------------------
# recovery / budget
# ---------------------------------------------------------------------------

def test_recovery_decision_forward_fix_preferred():
    """代码变更：前向修复通常比回滚便宜且安全。"""
    decision = recovery.recovery_decision(rollback_cost=30.0,
                                          forward_fix_cost=5.0,
                                          residual_risk=0.2)
    assert decision["decision"] == "forward_fix"
    assert "回归" in decision["rationale"]


def test_recovery_decision_rollback_when_cheap_and_ready():
    decision = recovery.recovery_decision(rollback_cost=2.0,
                                          forward_fix_cost=10.0,
                                          rollback_ready=True)
    assert decision["decision"] == "rollback"


def test_error_budget_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "budget.jsonl"
    recovery.consume_budget("高风险操作", cost=2.0, path=ledger)
    recovery.consume_budget("另一次", cost=1.0, path=ledger)
    status = recovery.budget_status(allowance=5.0, path=ledger)
    assert status["consumed"] == 3.0
    assert status["remaining"] == 2.0
    assert status["exhausted"] is False
    assert "budget_healthy" in status["verdict"]  # 剩余 40% ≥ 30%
    recovery.consume_budget("第三次", cost=1.0, path=ledger)
    status3 = recovery.budget_status(allowance=5.0, path=ledger)
    assert "budget_low" in status3["verdict"]  # 剩余 1/5 = 20% < 30%
    status2 = recovery.budget_status(allowance=2.0, path=ledger)
    assert status2["exhausted"] is True
    assert "强制降级" in status2["verdict"]


def test_budget_cli(tmp_path, monkeypatch, capsys):
    from pbatch.recovery import budget_main
    monkeypatch.setattr(recovery, "BUDGET_FILE", tmp_path / "b.jsonl")
    with pytest.raises(SystemExit) as exc:
        budget_main(["consume", "--reason", "x", "--cost", "1"])
    assert exc.value.code == 0
    capsys.readouterr()  # 清空 consume 输出
    with pytest.raises(SystemExit) as exc2:
        budget_main(["report", "--allowance", "0.5", "--json"])
    assert exc2.value.code == 1  # 耗尽 → 非零退出
    report = json.loads(capsys.readouterr().out)
    assert report["exhausted"] is True


def test_select_with_quality_floor():
    """§19：质量底线先于资源优化；无达标者 → infeasible_under_floor。"""
    from pbatch.pareto import select_with_quality_floor
    candidates = [
        {"id": "fast", "scores": {"quality": 0.3, "time": 1}},
        {"id": "solid", "scores": {"quality": 0.8, "time": 5}},
        {"id": "best", "scores": {"quality": 0.95, "time": 8}},
    ]
    # floor 0.6：fast 被排除；time 越小越好（minimize，权重取正）：
    # utility = quality − 0.5·time → solid(-1.7) vs best(-3.05) → solid
    chosen = select_with_quality_floor(
        candidates, "quality", 0.6, {"quality": 1.0, "time": 0.5},
        minimize={"time"})
    assert chosen["status"] == "feasible"
    assert chosen["id"] == "solid"
    # floor 0.99：无人达标 → 不降质量凑数
    infeasible = select_with_quality_floor(
        candidates, "quality", 0.99, {"quality": 1.0})
    assert infeasible["status"] == "infeasible_under_floor"
    assert infeasible["best_quality"] == 0.95


def test_probe_report_deterministic():
    """ProbeReport：确定性输出 + 区间估计 + 置信度（AADM-R §5）。"""
    from pbatch import probe
    report = probe.probe_report("写一个订单创建接口，含支付审批，生产，go")
    assert "probe_value" in report
    assert set(report["probe_value"]) == {"score", "inputs", "worth_probing"}
    assert report["recommended_mode"] in ("L0", "L1", "L2", "L3")
    assert report["recommended_graph"] in ("chain", "dag", "and-or-dag")
    estimates = report["estimates"]
    assert estimates["tokens"]["p50"] < estimates["tokens"]["p90"]
    assert 0 < estimates["tokens"]["confidence"] <= 1
    assert report["confidence"] > 0
    # 无 code_dir → unknowns 明确标注（不伪装成完整）
    assert any("code_dir" in u for u in report["unknowns"])


def test_probe_budget_infeasible(tmp_path):
    """§20：预算低于最低质量 → infeasible_under_budget + cannot_remove。"""
    from pbatch import probe
    report = probe.probe_report("写一个订单创建接口，含支付审批，生产，go")
    budget = probe._budget_check(report, budget_tokens=3000,
                                 budget_minutes=None)
    assert budget["status"] == "infeasible_under_budget"
    assert "tokens" in budget["violations"]
    assert "security_verification" in budget["cannot_remove"]
    # 充足预算 → feasible
    budget_ok = probe._budget_check(report, budget_tokens=10 ** 9,
                                    budget_minutes=None)
    assert budget_ok["status"] == "feasible"
