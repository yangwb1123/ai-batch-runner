"""Tests for the quantified task profile (pbatch/profile.py) — P1/P3/P4.

多维画像（12 维向量/自主性/L0-L3 路由/迟滞）、硬触发器、裁量包络、
假设记录与处理决策表。全部确定性启发式，无 LLM。
"""

from __future__ import annotations

from pbatch.profile import (assumption_records, task_profile,
                            _apply_hard_triggers, _discretion_envelope,
                            _mode_from_load, _autonomy_vector)


def test_mode_thresholds():
    assert _mode_from_load(0.10) == 0
    assert _mode_from_load(0.30) == 1
    assert _mode_from_load(0.60) == 2
    assert _mode_from_load(0.90) == 3


def test_hard_triggers_override_load():
    """硬触发器直接升级（auth/审计/生产数据删除），不受 Load 限制。"""
    assert _apply_hard_triggers(0, "增加 OAuth 登录认证") == 2
    assert _apply_hard_triggers(1, "执行数据库迁移") == 2
    assert _apply_hard_triggers(1, "执行生产数据库迁移") == 3  # 生产数据→L3
    assert _apply_hard_triggers(0, "跨项目公共契约变更") == 2
    assert _apply_hard_triggers(0, "支付退款与财务对账") == 3
    assert _apply_hard_triggers(0, "删除生产数据表") == 3
    assert _apply_hard_triggers(0, "普通按钮对齐") == 0


def test_ui_tweak_stays_low_mode():
    """订单页按钮对齐（领域名词而非业务动作）不应被抬到 L2/L3。"""
    report = task_profile("把订单页面顶部三个按钮对齐，并统一间距")
    assert report["mode"] <= 1
    assert report["profile"]["risk"] <= 0.2
    assert report["autonomy"]["execution"] >= 0.5


def test_approval_flow_high_risk():
    """审批+审计 → L3（硬触发器）；执行自主性低、学习自主性低。"""
    report = task_profile("在 snaplink-console 增加文件审批，审批后记录审计并通知申请人")
    assert report["mode"] == 3
    assert report["hard_triggers"], "审计应命中硬触发器"
    assert report["autonomy"]["execution"] <= 0.3
    assert report["autonomy"]["learning"] <= 0.2
    # 高不确定+高风险 → 行动探索度远低于规划探索度（AADM §16）
    assert report["action_exploration"] < report["planning_exploration"]


def test_irreversible_db_blocks_execution():
    report = task_profile("删除生产数据库订单表")
    assert report["mode"] == 3
    assert report["envelope"]["rollback_required"] is True
    assert report["envelope"]["human_gate_required"] is True
    assert "不可逆" in " ".join(report["envelope"]["forbidden_action_types"])


def test_envelope_required_proofs_scale_with_risk():
    low = task_profile("把订单页面顶部三个按钮对齐，并统一间距")
    assert low["envelope"]["required_proofs"] == ["test_pass"]
    high = task_profile("支付审批流程，生产环境")
    assert "impact_map" in high["envelope"]["required_proofs"]
    assert "rollback_plan" in high["envelope"]["required_proofs"]


def test_autonomy_vector_bounds():
    vector = _autonomy_vector({"risk": 0.9, "uncertainty": 0.8,
                               "reversibility": 0.1})
    assert 0 <= vector["execution"] <= 0.95
    assert 0.05 <= vector["learning"] <= 0.40
    assert vector["information"] > vector["planning"]


def test_profile_json_serializable():
    import json
    report = task_profile("企业订单管理：新建订单表单，含支付审批流程")
    json.dumps(report, ensure_ascii=False)  # 必须可 JSON 序列化
    assert set(report["profile"]) == {
        "clarity", "scope", "coupling", "risk", "uncertainty",
        "reversibility", "novelty", "testability", "evidence",
        "observability", "value", "time_pressure"}


def test_assumption_records_from_product_chain():
    records = assumption_records(
        "做一个开源上传产品，断点续传，商业化")
    assert records, "产品化 L3 推演链应产生假设记录"
    for record in records:
        assert 0 <= record["assumption_risk"] <= 1
        assert record["verification_plan"]
        assert record["status"] in ("accepted", "unverified")
    # 高风险假设不得"直接实现"
    assert all("确认" in r["verification_plan"] or "阻止" in r["verification_plan"]
               for r in records if r["assumption_risk"] > 0.35)


def test_assumption_decision_table_buckets():
    """风险分档决定处理方式（AADM §17 决策表）。"""
    records = assumption_records("做一个开源上传产品，断点续传，商业化")
    plans = {r["verification_plan"] for r in records}
    assert plans, "产品化推演链应产生待确认问题"


def test_profile_cli_json(capsys):
    import pytest
    from pbatch.profile import profile_main
    with pytest.raises(SystemExit) as exc:
        profile_main(["登录页面 demo", "--json"])
    assert exc.value.code == 0
    import json
    report = json.loads(capsys.readouterr().out)
    assert "mode" in report and "envelope" in report
