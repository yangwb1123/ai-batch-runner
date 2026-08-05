"""Tests for the backend spec domain: registry selection, backend rule
matching, backend assessment, the backend quality gate, and the review
pipeline."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pbatch.rule_matcher import domain_for, load_registry, match_rules

SCRIPT = "scripts/check-backend-quality.py"


def _run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, timeout=120)


def test_backend_domain_selection():
    assert domain_for("写一个 go 的订单接口") == "backend"
    assert domain_for("做一个登录页面") == "frontend_ui"


def test_backend_registry_loads_backend_rules():
    registry = load_registry(domain="backend")
    ids = set(registry["rules"])
    assert {"architecture", "design-patterns", "domain-modeling",
            "evolution"} <= ids
    # frontend rules never leak into the backend registry
    assert "visual-core" not in ids


def test_match_rules_uses_backend_registry_for_backend_tasks():
    matched = match_rules("写一个订单创建接口，含支付和审批，生产环境，go 实现")
    ids = [item["id"] for item in matched["rules"]]
    assert "architecture" in ids
    assert "domain-modeling" in ids  # risk-mandated on high-risk
    assert "visual-core" not in ids


def test_backend_risk_mandates_domain_modeling():
    matched = match_rules("写一个订单创建接口")
    assert matched["risk"] == "high"
    required = {item["id"] for item in matched["rules"] if item["required"]}
    assert "domain-modeling" in required


def test_backend_assessment_prescribes_backend_rules():
    from pbatch.assessor import prescription
    assessment = prescription("写一个订单创建接口，含支付和审批，生产环境，go 实现")
    ids = [item["id"] for item in assessment["prescription"]]
    assert "architecture" in ids
    assert "visual-core" not in ids
    assert "evolution" not in ids or assessment["effective_tier"] == "production"


def test_backend_checker_errors(tmp_path):
    bad = tmp_path / "order.ts"
    bad.write_text("this.status = 'paid';\nconsole.log('x');\n", encoding="utf-8")
    result = _run(str(bad))
    assert result.returncode == 1
    assert "direct status assignment" in result.stderr
    assert "console.log" in result.stderr


def test_backend_checker_domain_import_direction(tmp_path):
    domain = tmp_path / "domain" / "order.ts"
    domain.parent.mkdir(parents=True)
    domain.write_text("import { prisma } from '../../infrastructure/db';\n",
                      encoding="utf-8")
    result = _run(str(domain))
    assert result.returncode == 1
    assert "dependency direction" in result.stderr


def test_backend_checker_over_abstraction_warning(tmp_path):
    repo = tmp_path / "repo.ts"
    repo.write_text("export interface OrderRepo { find(): Promise<any>; }\n"
                    "export class OrderRepoImpl implements OrderRepo {\n"
                    "  find() { return Promise.resolve(null); }\n}\n",
                    encoding="utf-8")
    loose = _run(str(repo))
    assert loose.returncode == 0  # warnings only under --strict
    strict = _run(str(repo), "--strict")
    assert strict.returncode == 1
    assert "single-implementation interface" in strict.stderr


def test_backend_checker_empty_dir_passes(tmp_path):
    result = _run("--dir", str(tmp_path), "--strict")
    assert result.returncode == 0


def test_backend_quality_validator_resolves():
    from pbatch.config import _resolve_validators
    commands = _resolve_validators("backendquality")
    assert commands and "check-backend-quality.py --dir" in commands[0]
    assert "--strict" in commands[0]


def test_backend_review_pipeline_schema():
    from pbatch.pipeline import load_pipeline
    pipeline = load_pipeline("examples/backend-engineering-review.yaml")
    assert [s.name for s in pipeline.stages] == [
        "inventory", "engineering_review", "gate", "verdict"]
    assert pipeline.stages[2].validate_cmd == "backendquality"
    assert pipeline.stages[3].gate is True


def test_backend_role_keywords_registered():
    from pbatch.relevance import load_role_keywords
    keywords = load_role_keywords()
    for role in ("backend_engineer", "design_pattern_reviewer"):
        assert role in keywords
        assert keywords[role]


def test_backend_spec_files_exist():
    for file in ("architecture.md", "design-patterns.md",
                 "domain-modeling.md", "evolution.md", "rules.yaml"):
        assert (__import__("pathlib").Path("backend-specs") / file).exists()


def test_algorithms_rule_triggered_by_scenario():
    matched = match_rules("写一个商品搜索接口，Top K 排行，用 go 实现")
    ids = [item["id"] for item in matched["rules"]]
    assert "algorithms" in ids


def test_algorithms_rule_not_triggered_by_plain_crud():
    matched = match_rules("做一个客户管理的 CRUD 接口")
    ids = [item["id"] for item in matched["rules"]]
    assert "algorithms" not in ids


def test_ddd_and_complexity_only_at_production_tier():
    matched = match_rules("企业订单域：多实体复杂流程，微服务生产环境，含支付审批")
    ids = [item["id"] for item in matched["rules"]]
    assert matched["tier"] == "production"
    assert {"ddd", "complexity-scale", "evolution"} <= set(ids)
    small = match_rules("写一个订单接口")
    small_ids = [item["id"] for item in small["rules"]]
    assert "ddd" not in small_ids


def test_oop_di_rule_at_standard_tier():
    matched = match_rules("写一个订单创建接口")
    ids = [item["id"] for item in matched["rules"]]
    assert "oop-di" in ids


def test_backend_classifier_business_terms():
    from pbatch.classifier import classify_text
    assert classify_text("企业订单域：多实体复杂流程，微服务生产环境").task_type == "backend"
    assert classify_text("做一个订单支付页面").task_type == "frontend_ui"


def test_checker_n_plus_one_warning(tmp_path):
    bad = tmp_path / "svc.ts"
    bad.write_text(
        "for (const order of orders) {\n"
        "  const customer = await customerRepo.findById(order.customerId);\n"
        "}\n", encoding="utf-8")
    assert _run(str(bad)).returncode == 0  # warnings under strict only
    strict = _run(str(bad), "--strict")
    assert strict.returncode == 1
    assert "N+1 I/O risk" in strict.stderr


def test_checker_full_sort_topk_warning(tmp_path):
    bad = tmp_path / "rank.ts"
    bad.write_text("const top = rows.sort((a, b) => b.score - a.score).slice(0, 10);\n",
                   encoding="utf-8")
    strict = _run(str(bad), "--strict")
    assert strict.returncode == 1
    assert "Top K should use a heap" in strict.stderr


def test_backend_spec_registry_integrity():
    import yaml
    data = yaml.safe_load(Path("backend-specs/rules.yaml").read_text(encoding="utf-8"))
    missing = []
    for rule_id, rule in data["rules"].items():
        for file in rule.get("files", []):
            if not Path(file).exists():
                missing.append(f"{rule_id}: {file}")
    assert not missing, missing


def test_persistence_rule_risk_mandated():
    matched = match_rules("写一个订单创建接口，含支付")
    ids = [item["id"] for item in matched["rules"]]
    assert "persistence-modeling" in ids
    required = {item["id"] for item in matched["rules"] if item["required"]}
    assert "persistence-modeling" in required


def test_persistence_rule_not_at_demo_tier():
    matched = match_rules("做一个客户管理的 CRUD 接口")
    ids = [item["id"] for item in matched["rules"]]
    assert "persistence-modeling" not in ids


def test_production_rules_at_production_tier():
    matched = match_rules("企业订单域：多实体复杂流程，微服务生产环境，含支付审批")
    ids = [item["id"] for item in matched["rules"]]
    assert {"production-readiness", "persistence-modeling",
            "agent-guardrails"} <= set(ids)


def test_checker_float_money_error(tmp_path):
    sql = tmp_path / "schema.sql"
    sql.write_text("CREATE TABLE t (price DOUBLE, amount FLOAT);\n", encoding="utf-8")
    result = _run(str(sql), "--strict")
    assert result.returncode == 1
    assert "money stored as float/double" in result.stderr


def test_checker_select_star_and_tenant(tmp_path):
    sql = tmp_path / "q.sql"
    sql.write_text("SELECT * FROM orders WHERE id = ?;\n", encoding="utf-8")
    result = _run(str(sql), "--strict")
    assert result.returncode == 1
    assert "SELECT *" in result.stderr
    assert "tenant scoping" in result.stderr


def test_backend_engineer_prompt_requires_persistence_report():
    prompt = Path("prompts/backend_engineer.md").read_text(encoding="utf-8")
    assert "Persistence Design" in prompt
    assert "four models" in prompt.lower() or "API DTO" in prompt
