"""Tests for the backend spec domain: registry selection, backend rule
matching, backend assessment, the backend quality gate, and the review
pipeline."""

from __future__ import annotations

import json
import subprocess
import sys

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
