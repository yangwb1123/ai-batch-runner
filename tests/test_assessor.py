"""Tests for the requirement assessor (pbatch/assessor.py): completeness
dimensions, scale signals, restrained prescriptions, and the assess CLI."""

from __future__ import annotations

import json
import sys

from pbatch.assessor import (assess_dimensions, assess_main, format_assessment,
                             prescription, scale_signal)


def test_dimensions_present_and_missing():
    result = assess_dimensions("做订单表单，接口失败要重试，用 React 实现")
    present = {name for name, _, ok in result if ok}
    assert "goal" in present
    assert "data_source" in present
    assert "error_path" in present
    assert "tech_stack" in present
    assert "acceptance" not in present
    assert "user_role" not in present


def test_scale_signal():
    assert scale_signal("做一个登录页面") == "S"
    assert scale_signal("新建订单表单，含表格列表和分页") == "M"
    assert scale_signal("工作台+大屏，多模块多角色，含审批流和向导") == "L"


def test_prescription_is_restrained_by_scale():
    # "企业" keyword raises the matched tier, but a login page stays demo.
    assessment = prescription("做一个企业登录页面")
    assert assessment["matched_tier"] == "production"
    assert assessment["effective_tier"] == "demo"
    ids = [item["id"] for item in assessment["prescription"]]
    # Demo tier keeps the design-intelligence core (product thinking +
    # information priority are mandatory at every scale) plus visual core.
    assert ids == [
        "visual-core",
        "design-intelligence-product",
        "design-intelligence-priority",
        "animation-overshoot-clamp",
        "component-spec",
    ]
    excluded = {item["id"] for item in assessment["not_selected"]}
    assert "async-data" in excluded
    assert "architecture-budgets" in excluded
    assert "design-intelligence-emotional" in excluded


def test_prescription_full_requirement():
    assessment = prescription(
        "企业订单管理：新建订单表单，含支付审批流程，需要权限控制，"
        "接口失败要有重试和提示，验收需测试通过，用 React TSX 实现")
    assert assessment["effective_tier"] == "standard"
    ids = [item["id"] for item in assessment["prescription"]]
    assert "async-data" in ids
    assert "error-recovery" in ids
    assert "architecture-budgets" not in ids  # standard < production
    assert assessment["completeness"]["score"] >= 6


def test_under_specified_requirement_gets_suggestions():
    assessment = prescription("做一个页面")
    missing = assessment["completeness"]["missing"]
    assert "acceptance" in missing
    assert assessment["suggestions"]  # every missing dimension has advice


def test_assess_cli_human(capsys):
    sys.argv = ["pi-batch.py", "assess", "做一个登录页面"]
    assess_main(sys.argv[2:])
    out = capsys.readouterr().out
    assert "需求评估" in out
    assert "处方" in out
    assert "刻意未选用" in out


def test_assess_cli_json(capsys):
    sys.argv = ["pi-batch.py", "assess", "--json", "企业订单表单，支付审批，React"]
    assess_main(sys.argv[2:])
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale"] in ("M", "L")
    assert "prescription" in payload
    assert "not_selected" in payload
    assert "completeness" in payload


def test_assess_llm_json_reconciles(capsys):
    sys.argv = ["pi-batch.py", "assess", "企业支付审批，生产",
                "--llm-json", '{"apply":["visual-core"],"skip":["error-recovery"]}']
    assess_main(sys.argv[2:])
    out = capsys.readouterr().out
    assert "vetoed" in out or "必选" in out  # required rule survives the LLM skip


def test_format_assessment_contains_restraint_section():
    text = format_assessment(prescription("做一个登录页面"))
    assert "刻意未选用" in text
    assert "建议补充" in text
    assert "处方档 demo" in text
