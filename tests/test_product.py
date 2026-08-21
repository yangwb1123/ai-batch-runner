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


def test_explicit_local_tool_outweighs_broad_product_word():
    level, evidence = productization_level("写个内部产品小工具脚本")
    assert level == "L0_local_feature"
    assert {"内部", "小工具", "脚本"} & set(evidence)


def test_strong_commercial_signal_can_escalate_internal_tool():
    level, _ = productization_level("内部小工具，准备商业化并对外发布")
    assert level == "L3_product_feature"


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


def test_billing_scenario_surfaces_confirmation_questions():
    """商业机制出现时，先暴露计费决策问题，不直接推演实现。"""
    scenarios = scenario_questions("做一个 SaaS 平台，订阅收费，按配额计费")
    assert len(scenarios) == 1
    scenario = scenarios[0]
    assert "计费" in scenario["scenario"] or "订阅" in scenario["scenario"]
    assert len(scenario["questions"]) >= 3
    joined = " ".join(scenario["questions"])
    assert "定价" in joined and "计量" in joined
    assert "配额" in joined or "额度" in joined


def test_billing_scenario_keywords_align_with_product_registry():
    """计费链触发词必须来自声明式 billing use-case 注册表。"""
    from pbatch.product import _SCENARIOS
    from pbatch.rule_matcher import load_registry

    terms, _ = _SCENARIOS["计费/订阅"]
    registry_terms = set(load_registry(domain="product")["use_case_types"]["billing"])
    assert set(terms) <= registry_terms
    assert not {"收费", "付费", "支付", "商业化"}.intersection(terms)


def test_billing_scenario_does_not_break_l0_restraint():
    """L0 小工具仍不加载任何产品场景。"""
    manifest = product_manifest("做一个内部小工具脚本")
    assert manifest["level"] == "L0_local_feature"
    assert manifest["scenarios"] == []


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


def test_derived_specs_parity_l3_and_l0():
    """D3 parity pin: the registry-derived spec sets must equal the
    documented contract (L3 = the four product-spec files, L0 = []).
    Guards silent level-spec drift when the registry evolves."""
    from pbatch.product import _SPECS_BY_LEVEL
    assert _SPECS_BY_LEVEL["L0_local_feature"] == []
    assert sorted(_SPECS_BY_LEVEL["L3_product_feature"]) == sorted([
        "product-specs/product-thinking.md",
        "product-specs/commercial-readiness.md",
        "product-specs/open-source-readiness.md",
        "product-specs/completion-evidence.md",
    ])


def test_domain_for_routes_product_and_generic():
    """F6 pin: L1+ productization routes to the product domain; L0 stays
    generic (restraint); backend/frontend precedence preserved."""
    from pbatch.rule_matcher import domain_for
    assert domain_for("做一个开源上传产品，商业化") == "product"
    assert domain_for("写个内部小工具脚本，解析日志") == "generic"
    assert domain_for("写一个订单创建接口，含支付审批，生产，go") == "backend"
    assert domain_for("生成一个登录页面 tsx") == "frontend_ui"
