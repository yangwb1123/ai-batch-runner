"""Tests for 规则晋升闭环 / 五类指标 / 调度优先级 / 复杂度模型 /
StopPolicy / 不确定性分类 / 多样性评分。

learn promote: 草案并入注册表（RCA 前置/文件存在/重名拒绝/fail closed）。
metrics: 五类指标聚合形状与有界读取。
waves: priority_score 排序、structural_complexity 关系/状态主导。
profile: stop_policy 停止判定、三类不确定性分类。
diversity: 相同 Agent=0 独立证据，多样 Agent=1。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import diversity, learn, metrics, profile
from pbatch.memory_policy import memory_scope
from pbatch.fabric_sched import TaskNode
from pbatch.waves import (priority_score, rank_nodes, structural_complexity)


# ---------------------------------------------------------------------------
# learn promote：规则生命周期闭环
# ---------------------------------------------------------------------------

def _rca_draft(tmp_path, rule_id):
    """带 RCA 的草案（requirements 已填）。"""
    draft = learn.rule_schema_template()
    draft["id"] = rule_id
    draft["title"] = "并发下单必须幂等"
    draft["requirements"] = ["唯一约束覆盖订单号"]
    draft["verification"]["tests"] = ["并发测试"]
    draft["trigger"]["operations"] = ["并发", "重复订单"]
    path = tmp_path / f"{rule_id}-20260101.yaml"
    from pbatch.config import yaml
    path.write_text(yaml.safe_dump(draft, allow_unicode=True), encoding="utf-8")
    return path


def test_promote_draft_merges_into_registry(tmp_path, monkeypatch):
    rule_id = "PROMO-001"
    draft_path = _rca_draft(tmp_path, rule_id)
    registry = tmp_path / "rules.yaml"
    registry.write_text("rules:\n  existing-rule:\n"
                        "    description: x\n    files: [a.md]\n"
                        "    min_tier: demo\n", encoding="utf-8")
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("spec", encoding="utf-8")
    monkeypatch.setattr(learn, "DRAFT_DIR", tmp_path)
    monkeypatch.setattr(learn, "PROMOTED_DIR", tmp_path / "promoted")
    result, moved = learn.promote_draft(
        rule_id, files=[str(spec_file)], min_tier="standard",
        draft_dir=tmp_path, registry_path=str(registry))
    assert result == str(registry)
    text = registry.read_text(encoding="utf-8")
    assert f"  {rule_id}:" in text
    assert "promoted from draft" in text
    assert moved and Path(moved).exists()  # 草案已存档
    assert not draft_path.exists()         # 原草案已移走


def test_promote_requires_rca(tmp_path, monkeypatch):
    """未填 RCA 的草案拒绝提升（fail closed）。"""
    draft = learn.rule_schema_template()
    draft["id"] = "NO-RCA"
    (tmp_path / "NO-RCA-20260101.yaml").write_text(
        "id: NO-RCA\ntitle: x\n", encoding="utf-8")
    monkeypatch.setattr(learn, "DRAFT_DIR", tmp_path)
    with pytest.raises(ValueError) as exc:
        learn.promote_draft("NO-RCA", files=["ui-specs/spacing.md"],
                            draft_dir=tmp_path,
                            registry_path=str(tmp_path / "r.yaml"))
    assert "RCA" in str(exc.value)


def test_promote_rejects_missing_files_and_duplicates(tmp_path, monkeypatch):
    rule_id = "PROMO-002"
    _rca_draft(tmp_path, rule_id)
    monkeypatch.setattr(learn, "DRAFT_DIR", tmp_path)
    with pytest.raises(ValueError) as exc:
        learn.promote_draft(rule_id, files=["no-such-file.md"],
                            draft_dir=tmp_path,
                            registry_path=str(tmp_path / "r.yaml"))
    assert "missing" in str(exc.value)
    registry = tmp_path / "r.yaml"
    registry.write_text(f"  {rule_id}:\n    description: dup\n",
                        encoding="utf-8")
    spec = tmp_path / "s.md"
    spec.write_text("s", encoding="utf-8")
    with pytest.raises(ValueError) as exc2:
        learn.promote_draft(rule_id, files=[str(spec)],
                            draft_dir=tmp_path, registry_path=str(registry))
    assert "already exists" in str(exc2.value)


# ---------------------------------------------------------------------------
# metrics：五类指标
# ---------------------------------------------------------------------------

def test_metrics_report_shape():
    report = metrics.metrics_report()
    for category in ("result_quality", "decision_quality",
                     "scheduling_quality", "collaboration_quality",
                     "resource_quality"):
        assert category in report
    assert isinstance(report["result_quality"]["rejected_artifacts"], int)
    assert "note" in report["collaboration_quality"]  # 诚实标注未追踪


def test_metrics_report_json_serializable():
    json.dumps(metrics.metrics_report())


def test_metrics_bounded_scan(tmp_path, monkeypatch):
    """超长行/超大索引被有界跳过（不 OOM）。"""
    index = tmp_path / "index.jsonl"
    index.write_text("x" * (metrics.INDEX_LINE_MAX + 10) + "\n"
                     + json.dumps({"observed_verdict": "PASS",
                                   "scope": memory_scope()}) + "\n",
                     encoding="utf-8")
    monkeypatch.setattr(metrics, "MEMORY_INDEX", index)
    stats = metrics._scan_memory_index()
    assert stats["sessions"] == 1
    assert stats["passed"] == 1


# ---------------------------------------------------------------------------
# waves：调度优先级 + 结构复杂度
# ---------------------------------------------------------------------------

def _node(nid, goal="goal", deps=(), wset=(), est=10.0, effect="read_only"):
    return TaskNode(id=nid, goal=goal, argv=["echo", nid],
                    depends_on=list(deps), write_set=list(wset),
                    estimate_seconds=est, effect_class=effect)


def test_priority_score_unblock_and_risk():
    """解锁价值高的节点（多节点依赖它）与高风险节点应优先。"""
    hub = _node("hub", deps=(), est=5.0)
    leaf_a = _node("a", deps=("hub",), est=1.0)
    leaf_b = _node("b", deps=("hub",), est=1.0)
    risky = _node("risky", wset=("x",), effect="irreversible")
    nodes = [hub, leaf_a, leaf_b, risky]
    assert priority_score(hub, nodes) > priority_score(leaf_a, nodes)
    assert priority_score(risky, nodes) > priority_score(leaf_a, nodes)


def test_rank_nodes_order():
    nodes = [_node("low", est=1.0), _node("high", deps=("low",), est=50.0)]
    ranked = rank_nodes([nodes[1], nodes[0]], nodes)
    assert ranked[0].id == "high"  # 关键路径长 → 优先


def test_structural_complexity_counts_relations():
    """复杂度来自关系与共享状态：节点少但共享写状态多 → 复杂度高。"""
    simple = [_node("a", wset=("x",)), _node("b", wset=("y",)),
              _node("c", wset=("z",))]
    tangled = [_node("a", wset=("shared",)), _node("b", wset=("shared",)),
               _node("c", wset=("shared",))]
    simple_report = structural_complexity(simple)
    tangled_report = structural_complexity(tangled)
    assert tangled_report["shared_mutable_states"] == 1
    assert simple_report["shared_mutable_states"] == 0
    assert tangled_report["complexity"] > simple_report["complexity"]


def test_structural_complexity_cross_boundary():
    nodes = [_node("a", goal="frontend"), _node("b", goal="backend",
                                                deps=("a",))]
    report = structural_complexity(nodes)
    assert report["cross_boundary_edges"] == 1


# ---------------------------------------------------------------------------
# profile：StopPolicy + 不确定性分类
# ---------------------------------------------------------------------------

def test_uncertainty_types_classification():
    types = profile.uncertainty_types(
        "接口是否存在未知；网络延迟会波动；用户输入可能注入")
    assert "未知" in types["epistemic"]
    assert "延迟" in types["aleatoric"]
    assert "注入" in types["adversarial"]
    assert profile.uncertainty_types("简单任务") == {
        "epistemic": [], "aleatoric": [], "adversarial": []}


def test_stop_policy_in_profile():
    report = profile.task_profile("在 snaplink-console 增加文件审批，审批后记录审计并通知申请人")
    policy = report["stop_policy"]
    assert policy["acceptance_coverage_required"] == 1.0
    assert policy["max_no_progress_cycles"] == 3
    assert "infeasible_under_budget" in policy["honest_states"]


def test_should_stop_conditions():
    policy = {"acceptance_coverage_required": 1.0, "max_critical_open_risks": 0,
              "max_residual_risk": 0.15, "require_rollback_ready": True,
              "require_independent_evidence": False, "max_no_progress_cycles": 3}
    stop, reasons = profile.should_stop(
        {"acceptance_coverage": 0.8, "critical_open_risks": 1,
         "residual_risk": 0.2, "rollback_ready": False,
         "no_progress_cycles": 0}, policy)
    assert stop is False and len(reasons) >= 3
    stop2, reasons2 = profile.should_stop(
        {"acceptance_coverage": 1.0, "critical_open_risks": 0,
         "residual_risk": 0.1, "rollback_ready": True,
         "no_progress_cycles": 1}, policy)
    assert stop2 is True and reasons2 == []


def test_should_stop_no_progress_escalates():
    """连续无进展：停止不是终点，应更换策略（不无限重试同一方案）。"""
    policy = {"acceptance_coverage_required": 1.0, "max_critical_open_risks": 1,
              "max_residual_risk": 0.3, "require_rollback_ready": False,
              "require_independent_evidence": False, "max_no_progress_cycles": 3}
    stop, reasons = profile.should_stop(
        {"acceptance_coverage": 1.0, "critical_open_risks": 0,
         "residual_risk": 0.1, "no_progress_cycles": 3}, policy)
    assert stop is False
    assert any("无进展" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# diversity：多 Agent 独立性
# ---------------------------------------------------------------------------

def test_identical_agents_zero_independence():
    """相同模型+方法+数据+Oracle 的 Agent 是复制错误，不是多份证据。"""
    agents = [{"model": "pi", "method": "implement", "data": "repo",
               "oracle": "tests"}] * 3
    report = diversity.diversity_score(agents)
    assert report["score"] == 0.0
    assert report["shared_failure_correlation"] == 1.0
    assert report["adequate_independence"] is False


def test_diverse_agents_full_independence():
    agents = [
        {"model": "pi", "method": "implement", "data": "repo",
         "oracle": "tests"},
        {"model": "claude", "method": "review", "data": "repo-v2",
         "oracle": "contracts"},
        {"model": "gemini", "method": "spike", "data": "repo-v3",
         "oracle": "fuzz"},
    ]
    report = diversity.diversity_score(agents)
    assert report["score"] >= 0.9
    assert report["shared_failure_correlation"] == 0.0


def test_partial_diversity():
    """模型不同 + 方法相同：公式只惩罚"同模型+同方法"（不同模型失败模式
    不同），因此得 1.0；方法共享本身不算相关错误。"""
    agents = [
        {"model": "pi", "method": "implement", "data": "repo",
         "oracle": "tests"},
        {"model": "claude", "method": "implement", "data": "repo",
         "oracle": "tests"},
    ]
    report = diversity.diversity_score(agents)
    assert report["score"] == 1.0
    assert report["shared_failure_correlation"] == 0.0
    assert report["dimensions"]["method"] == 0.0  # 方法维度无差异


def test_diversity_cli(tmp_path, capsys):
    from pbatch.diversity import diversity_main
    agents = '[{"model":"a","method":"m"},{"model":"a","method":"m"}]'
    with pytest.raises(SystemExit) as exc:
        diversity_main([agents, "--json"])
    assert exc.value.code == 1  # 独立性不足 → 非零退出
    assert json.loads(capsys.readouterr().out)["score"] == 0.0
