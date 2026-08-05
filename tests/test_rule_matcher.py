"""Tests for the deterministic rule matcher (pbatch/rule_matcher.py) and the
`pi-batch rules` subcommand: scale tiers, page-type/risk matching, bounded
manifests, and registry integrity."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pbatch.rule_matcher import (format_manifest, load_registry, match_rules,
                                 rules_main)


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
