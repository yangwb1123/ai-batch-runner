"""Tests for the deterministic rule matcher (pbatch/rule_matcher.py) and the
`pi-batch rules` subcommand: scale tiers, page-type/risk matching, bounded
manifests, and registry integrity."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pbatch.rule_matcher import (format_manifest, format_llm_prompt,
                                 load_registry, match_rules, reconcile,
                                 rules_main, summarize_task)


def test_demo_tier_loads_only_visual_core():
    matched = match_rules("做一个登录页面 demo")
    assert matched["tier"] == "demo"
    ids = [item["id"] for item in matched["rules"]]
    assert "visual-core" in ids
    assert "component-spec" in ids
    # engineering rules are gated behind the standard tier
    assert "async-data" not in ids
    assert "architecture-budgets" not in ids


def test_production_high_risk_loads_engineering_rules():
    matched = match_rules("企业订单管理：新建订单表单+订单表格，含支付审批")
    assert matched["tier"] == "production"
    assert matched["risk"] == "high"
    ids = [item["id"] for item in matched["rules"]]
    for required in ("visual-core", "async-data", "error-recovery",
                     "defect-patterns", "form-table-state",
                     "architecture-budgets"):
        assert required in ids, required


def test_default_tier_is_standard():
    matched = match_rules("客户管理页面，含列表和编辑表单")
    assert matched["tier"] == "standard"
    ids = [item["id"] for item in matched["rules"]]
    assert "async-data" in ids
    assert "architecture-budgets" not in ids  # production only


def test_page_type_gating():
    matched = match_rules("做一个订单列表页（表格+分页）")
    assert "table" in matched["page_types"]
    ids = [item["id"] for item in matched["rules"]]
    assert "layout-patterns" in ids
    assert "form-table-state" in ids
    auth = match_rules("登录页")
    assert "form" not in auth["page_types"] or "auth" in auth["page_types"]


def test_business_profile_resolves_template():
    matched = match_rules("做一个 ERP 排程页面")
    profile_rules = [item for item in matched["rules"] if item["id"] == "business-profile"]
    assert profile_rules
    assert profile_rules[0]["files"] == ["ui-specs/business-profiles/erp.md"]


def test_manifest_is_bounded_and_marks_truncation():
    matched = match_rules("企业 ERP：排程表单、工单表格、库存审批、支付，生产环境")
    manifest = format_manifest(matched, limit=3)
    assert "and 1 more rule(s)" in manifest or "and 2 more rule(s)" in manifest or "more rule(s)" in manifest
    assert "Apply ONLY these rules" in manifest


def test_rules_subcommand_json(capsys):
    sys.argv = ["pi-batch.py", "rules", "--json", "做一个登录页 demo"]
    rules_main(sys.argv[2:])
    payload = json.loads(capsys.readouterr().out)
    assert payload["tier"] == "demo"
    assert payload["rules"][0]["id"] == "visual-core"


def test_rules_subcommand_human(capsys):
    sys.argv = ["pi-batch.py", "rules", "企业支付页，生产"]
    rules_main(sys.argv[2:])
    out = capsys.readouterr().out
    assert "Applicable UI rules" in out
    assert "async-data" in out


def test_registry_integrity_all_files_exist():
    """Every rule file referenced by ui-specs/rules.yaml must exist."""
    import yaml
    registry_path = Path("ui-specs/rules.yaml")
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    missing = []
    for rule_id, rule in data["rules"].items():
        for file in rule.get("files", []):
            if not Path(file).exists():
                missing.append(f"{rule_id}: {file}")
        template = rule.get("files_template")
        if template:
            for profile in rule.get("profiles", []):
                path = template.replace("{profile}", profile)
                if not Path(path).exists():
                    missing.append(f"{rule_id}: {path}")
    assert not missing, missing


def test_registry_keyword_sections_present():
    registry = load_registry()
    assert {"demo", "production"} <= set(registry["scale"])
    assert "rules" in registry
    assert "visual-core" in registry["rules"]


def test_summarize_task_compresses_requirement():
    summary = summarize_task("企业订单管理系统：新建订单表单，含支付审批流程，生产环境上线")
    assert "scale=production" in summary
    assert "risk=high" in summary
    assert "page_types=form" in summary
    assert "原文" in summary
    long = summarize_task("做 " + "很长的需求描述" * 200)
    assert "原文截断" in long


def test_reconcile_agreed_and_llm_added():
    algorithm = match_rules("登录页 demo")
    final = reconcile(algorithm, ["visual-core", "component-spec", "async-data"], [])
    provenance = final["provenance"]
    assert provenance["visual-core"] == "both"
    assert provenance["async-data"] == "llm-added"
    ids = [item["id"] for item in final["rules"]]
    assert "async-data" in ids


def test_reconcile_vetoes_required_skip_and_drops_optional():
    algorithm = match_rules("企业支付审批，生产")
    final = reconcile(algorithm, ["visual-core"],
                      ["visual-core", "error-recovery", "architecture-budgets"])
    provenance = final["provenance"]
    assert provenance["visual-core"] == "algorithm-vetoed-llm-skip"
    assert provenance["error-recovery"] == "algorithm-vetoed-llm-skip"
    ids = [item["id"] for item in final["rules"]]
    assert "visual-core" in ids and "error-recovery" in ids
    # optional architecture-budgets IS dropped when the LLM skips it
    assert "architecture-budgets" not in ids
    assert any(item["id"] == "architecture-budgets" for item in final["dropped"])


def test_reconcile_ignores_unknown_rule_ids():
    algorithm = match_rules("登录页 demo")
    final = reconcile(algorithm, ["visual-core", "no-such-rule"], [])
    assert "no-such-rule" not in final["provenance"]


def test_risk_mandated_rules_are_required():
    algorithm = match_rules("企业支付审批，生产")
    required = {item["id"] for item in algorithm["rules"] if item["required"]}
    assert {"error-recovery", "defect-patterns"} <= required


def test_llm_prompt_contains_both_sides(capsys):
    sys.argv = ["pi-batch.py", "rules", "登录页 demo", "--llm-prompt", "/tmp/llm-check.txt"]
    rules_main(sys.argv[2:])
    prompt = Path("/tmp/llm-check.txt").read_text(encoding="utf-8")
    assert "COMPRESSED REQUIREMENT" in prompt
    assert "ALGORITHM MANIFEST" in prompt
    assert '"apply"' in prompt


def test_llm_json_flag_reconciles(capsys):
    sys.argv = ["pi-batch.py", "rules", "登录页 demo", "--llm-json",
                '{"apply":["visual-core","async-data"],"skip":[],"reason":"x"}']
    rules_main(sys.argv[2:])
    out = capsys.readouterr().out
    assert "llm-added" in out
    assert "async-data" in out


def test_invalid_llm_json_fails_closed():
    sys.argv = ["pi-batch.py", "rules", "登录页", "--llm-json", "not json"]
    try:
        rules_main(sys.argv[2:])
        raised = False
    except SystemExit as exc:
        raised = exc.code == 2
    assert raised


def test_interaction_patterns_rule_triggered_on_form_table():
    matched = match_rules("企业订单管理：新建订单表单，含支付审批流程")
    ids = [item["id"] for item in matched["rules"]]
    assert "interaction-patterns" in ids


def test_interaction_patterns_rule_skipped_for_demo_auth():
    matched = match_rules("做一个登录页 demo")
    ids = [item["id"] for item in matched["rules"]]
    assert "interaction-patterns" not in ids
    skipped = {item["id"] for item in matched["skipped"]}
    assert "interaction-patterns" in skipped
