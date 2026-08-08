"""Tests for Reflection Engine（Critic Layer / 二阶观察层）.

12 维度检查：目标对齐 / 需求完整性 / 假设审计 / 架构循环 / 复杂度 /
失败路径 / 安全 / 性能 / UX / 未来影响 / 反方验证 / 知识提取。
强度分级 R0/R1/R2、评分、闭环动作、CLI。
"""

from __future__ import annotations

import json

import pytest

from pbatch import reflect


def test_goal_alignment_missing():
    findings = reflect._goal_alignment_check(
        "提升订单查询速度", "实现了一个新的按钮")
    assert any(f["type"] == "goal_alignment"
               and "订单查询" in f["title"] for f in findings)


@pytest.mark.unit
def test_goal_alignment_aligned():
    assert reflect._goal_alignment_check(
        "提升订单查询速度", "查询接口加索引，P95 从 800ms 降至 120ms") == []


def test_completeness_missing_dimensions():
    findings = reflect._completeness_check("把订单页按钮对齐")
    types = {f["type"] for f in findings}
    assert "missed_requirement" in types
    assert any("permission" in f["title"] for f in findings)


def test_assumption_audit_unverified():
    findings = reflect._assumption_audit("做一个开源上传产品，断点续传", "")
    assert findings
    assert all(f["type"] == "wrong_assumption" for f in findings)
    assert all(f["severity"] in ("warning", "critical") for f in findings)


def test_assumption_audit_verified_with_evidence():
    findings = reflect._assumption_audit(
        "做一个开源上传产品，断点续传",
        "断点续传测试通过，失败重试验证完成")
    titles = " ".join(f["title"] for f in findings)
    assert "断点续传" not in titles and "失败重试" not in titles  # 已验证
    assert "秒传" in titles  # 未验证的仍被审计


def test_architecture_cycle_finding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pbatch").mkdir()
    (tmp_path / "pbatch" / "a.py").write_text("from .b import x\n",
                                              encoding="utf-8")
    (tmp_path / "pbatch" / "b.py").write_text("from .a import x\n",
                                              encoding="utf-8")
    findings = reflect._architecture_check(str(tmp_path / "pbatch"))
    assert any(f["type"] == "architecture" and f["severity"] == "critical"
               for f in findings)


def test_architecture_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pbatch").mkdir()
    (tmp_path / "pbatch" / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert reflect._architecture_check(str(tmp_path / "pbatch")) == []


def test_failure_mode_missing():
    findings = reflect._failure_mode_check("把按钮对齐")
    assert any(f["type"] == "missing_failure_mode" for f in findings)
    assert reflect._failure_mode_check("导入失败时重试并降级") == []


def test_security_risk_on_sensitive_domain():
    findings = reflect._security_check("支付退款与审批流程，生产")
    assert any(f["type"] == "security_risk" and f["severity"] == "critical"
               for f in findings)
    assert reflect._security_check("支付退款需权限矩阵与审计") == []


def test_performance_issue_on_export():
    findings = reflect._performance_check("增加订单导出功能")
    assert any(f["type"] == "performance_issue" for f in findings)
    assert reflect._performance_check("异步导出大文件，分页") == []


def test_ux_check_frontend_only():
    assert reflect._ux_check("做一个登录页")  # frontend 无反馈词 → finding
    assert reflect._ux_check("写一个订单查询接口") == []  # 后端不查 UX


def test_future_debt_signal():
    findings = reflect._future_impact_check("临时硬编码字段，先这样", "")
    assert any(f["type"] == "future_debt" for f in findings)


def test_adversarial_gap_only_positive_evidence():
    assert reflect._adversarial_check("所有测试通过，运行正常")
    assert reflect._adversarial_check("测试通过，边界测试发现拒绝路径") == []


def test_knowledge_extraction_from_findings():
    findings = [{"type": "missed_requirement", "severity": "warning"},
                {"type": "missing_failure_mode", "severity": "warning"}]
    rules = reflect._knowledge_extraction(findings)
    ids = {r["rule_id"] for r in rules}
    assert "REFLECT-COMPLETENESS" in ids
    assert "REFLECT-FAILURE-PATH" in ids
    assert reflect._knowledge_extraction([]) == []


def test_reflect_on_quick_mode():
    report = reflect.reflect_on("把订单页按钮对齐", mode="quick")
    assert report["mode"] == "quick"
    assert report["checked_dimensions"] == list(reflect.CHECKS_R0)
    assert 0 <= report["reflection_score"] <= 100
    assert report["grade"] in ("A", "B", "C", "D")
    assert "knowledge_updates" in report


@pytest.mark.scenario
def test_reflect_on_full_mode_with_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pbatch").mkdir()
    (tmp_path / "pbatch" / "a.py").write_text("x = 1\n", encoding="utf-8")
    report = reflect.reflect_on("支付审批导出，生产", code_dir=str(tmp_path / "pbatch"),
                                mode="full")
    assert "architecture" in report["checked_dimensions"]
    assert "security" in report["checked_dimensions"]
    assert any(f["type"] == "security_risk" for f in report["findings"])


def test_reflect_cli_json(capsys):
    with pytest.raises(SystemExit) as exc:
        reflect.reflect_main(["quick", "--task", "把订单页按钮对齐", "--json"])
    assert exc.value.code in (0, 1)
    report = json.loads(capsys.readouterr().out)
    assert "findings" in report and "reflection_score" in report


def test_reflect_cli_save(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reflect, "REFLECTIONS_FILE",
                        tmp_path / "reflections.jsonl")
    with pytest.raises(SystemExit):
        reflect.reflect_main(["quick", "--task", "把按钮对齐", "--save"])
    records = tmp_path / "reflections.jsonl"
    assert records.exists()
    first = json.loads(records.read_text(encoding="utf-8").splitlines()[0])
    assert "findings" in first


def test_reflection_score_penalties():
    """critical 20 / warning 8 / info 3 扣分。"""
    report = reflect.reflect_on("支付退款审批，生产环境删除数据")
    critical = [f for f in report["findings"]
                if f["severity"] == "critical"]
    assert critical
    assert report["reflection_score"] < 80
