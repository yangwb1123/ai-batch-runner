"""Tests for the productization assessment (pbatch/product.py) and its
integration into `pi-batch assess`."""

from __future__ import annotations

import json
import sys

from pbatch.assessor import assess_main, prescription
from pbatch.product import (format_product, product_manifest,
                            productization_level, scenario_questions)


def test_level_local_tool():
    level, evidence = productization_level("写个内部小工具脚本，解析日志")
    assert level == "L0_local_feature"
    assert "内部" in evidence


def test_level_reusable_module():
    level, _ = productization_level("做一个通用文件上传模块，给多个项目复用")
    assert level == "L1_reusable_module"


def test_level_platform():
    level, _ = productization_level("构建多租户平台，开放 API 与 Webhook，支持插件")
    assert level == "L2_platform_capability"


def test_level_product():
    level, _ = productization_level("做一个开源产品，商业化收费，上线发布")
    assert level == "L3_product_feature"


def test_l0_gets_no_scenarios_and_no_specs():
    manifest = product_manifest("写个临时脚本")
    assert manifest["level"] == "L0_local_feature"
    assert manifest["scenarios"] == []
    assert manifest["specs"] == []
    assert any("禁止产品化结构" in note for note in manifest["restraint_notes"])


def test_l3_gets_scenarios_specs_and_restraint():
    manifest = product_manifest("做一个开源文件上传产品，断点续传，多租户，商业化")
    assert manifest["level"] == "L3_product_feature"
    scenarios = {s["scenario"] for s in manifest["scenarios"]}
    assert "上传/文件" in scenarios
    assert "product-specs/open-source-readiness.md" in manifest["specs"]
    notes = " ".join(manifest["restraint_notes"])
    assert "低成本预留" in notes
    assert "待确认" in notes


def test_commercial_l3_without_open_source_intent_skips_open_source_spec():
    manifest = product_manifest("面向企业发布一个商业化 SaaS 产品并上线收费")
    assert manifest["level"] == "L3_product_feature"
    assert "product-specs/commercial-readiness.md" in manifest["specs"]
    assert "product-specs/open-source-readiness.md" not in manifest["specs"]
    assert any("未检测到开源意图" in note
               for note in manifest["restraint_notes"])


def test_login_scenario_chain():
    questions = scenario_questions("给系统加登录功能")
    assert any("权限/RBAC" in s["questions"] for s in questions)


def test_assess_includes_product_dimension():
    assessment = prescription("做一个开源登录产品，商业化")
    assert assessment["product"]["level"] == "L3_product_feature"
    assert assessment["product"]["scenarios"]


def test_assess_cli_product_human(capsys):
    sys.argv = ["pi-batch.py", "assess", "做一个开源上传产品，断点续传"]
    assess_main(sys.argv[2:])
    out = capsys.readouterr().out
    assert "产品化:" in out
    assert "推演" in out


def test_assess_cli_product_json(capsys):
    sys.argv = ["pi-batch.py", "assess", "--json", "做一个开源上传产品，断点续传"]
    assess_main(sys.argv[2:])
    payload = json.loads(capsys.readouterr().out)
    assert payload["product"]["level"] == "L3_product_feature"
    assert payload["product"]["specs"]


def test_format_product_contains_level_and_restraint():
    text = format_product(product_manifest("内部小工具"))
    assert "L0_local_feature" in text
    assert "克制" in text


def test_product_thinker_keywords_registered():
    from pbatch.relevance import load_role_keywords
    keywords = load_role_keywords()
    assert "product_thinker" in keywords
    assert keywords["product_thinker"]


def test_product_specs_exist():
    for file in ("product-thinking.md", "commercial-readiness.md",
                 "open-source-readiness.md", "completion-evidence.md"):
        assert (__import__("pathlib").Path("product-specs") / file).exists()
