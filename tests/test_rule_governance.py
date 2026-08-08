"""Tests for P2 (规则五层 + 激活/抑制) and P5/P6 (learn 例外 + 影子模式).

规则层级：invariant 永不跳过、激活度 1.0；suppress_on 显式抑制；
schema 校验 level/suppress_on。learn：例外记录可解释/可回收；影子模式
在日志上计算命中率并给裁决。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import learn
from pbatch.rule_cli import _check_rule
from pbatch.rule_matcher import (LEVELS, match_rules,
                                 _rule_skip_reason)

REGISTRY = {
    "scale": {"demo": ["demo"], "production": ["生产"]},
    "page_types": {"form": ["表单"]},
    "risk": {"high": ["审批"]},
    "signals": {}, "signal_weights": {}, "use_case_types": {},
    "rules": {
        "constitution-rule": {
            "description": "宪法规则", "files": ["ui-specs/spacing.md"],
            "min_tier": "production", "level": "invariant",
        },
        "policy-rule": {
            "description": "策略规则", "files": ["ui-specs/spacing.md"],
            "min_tier": "demo",
        },
        "suppressed-rule": {
            "description": "demo 被抑制", "files": ["ui-specs/spacing.md"],
            "min_tier": "demo", "suppress_on": ["demo"],
        },
        "required-suppressed-rule": {
            "description": "必选规则不受抑制", "files": ["ui-specs/spacing.md"],
            "min_tier": "demo", "required": True, "suppress_on": ["demo"],
        },
    },
}


def test_invariant_never_skipped_even_below_tier():
    """宪法层：即使 tier 不满足也必加载（违反即淘汰，不参与权重竞争）。"""
    matched = match_rules("demo 表单页", registry=REGISTRY)
    ids = [r["id"] for r in matched["rules"]]
    assert "constitution-rule" in ids
    item = next(r for r in matched["rules"] if r["id"] == "constitution-rule")
    assert item["level"] == "invariant"
    assert item["activation"] == 1.0
    assert item["required"] is True


def test_suppress_on_demo():
    """suppress_on 命中 → 显式抑制（可选规则）；必选规则不受抑制。"""
    matched = match_rules("demo 表单页", registry=REGISTRY)
    ids = [r["id"] for r in matched["rules"]]
    assert "suppressed-rule" not in ids
    assert "required-suppressed-rule" in ids
    reasons = {s["id"]: s["reason"] for s in matched["skipped"]}
    assert "suppressed" in reasons.get("suppressed-rule", "")


def test_rule_skip_reason_invariant_bypass():
    assert _rule_skip_reason({"level": "invariant"}, "demo", 0, [], False, "") == ""


def test_check_rule_validates_level_and_suppress_on():
    violations = []
    _check_rule("bad-level", {"description": "x", "min_tier": "demo",
                              "files": ["ui-specs/spacing.md"],
                              "level": "constitution"}, violations)
    assert any("invalid level" in v for v in violations)
    violations = []
    _check_rule("bad-suppress", {"description": "x", "min_tier": "demo",
                                 "files": ["ui-specs/spacing.md"],
                                 "suppress_on": ["bogus-signal"]}, violations)
    assert any("suppress_on" in v for v in violations)
    violations = []
    _check_rule("ok", {"description": "x", "min_tier": "demo",
                       "files": ["ui-specs/spacing.md"],
                       "level": "policy", "suppress_on": ["demo"]}, violations)
    assert not violations


def test_real_registry_has_invariant_levels():
    """真实注册表：visual-core 已标 invariant，激活度 1.0。"""
    matched = match_rules("登录页 demo")
    core = next((r for r in matched["rules"] if r["id"] == "visual-core"), None)
    assert core is not None
    assert core["level"] == "invariant"
    assert core["activation"] == 1.0


def test_levels_enum():
    assert LEVELS == ("invariant", "contract", "policy", "heuristic",
                      "suggestion")


# ---------------------------------------------------------------------------
# P5: 软规则例外
# ---------------------------------------------------------------------------

def test_exception_record_schema():
    record = learn.exception_record(
        "FE-COMPLEXITY-012", "性能关键解析器拆分反而增加分配开销",
        expected_benefit=3, added_risk=1,
        compensating=["benchmark_test", "complexity_waiver"],
        scope=["src/parser.py"])
    assert record["rule_id"] == "FE-COMPLEXITY-012"
    assert record["expected_benefit"] == 3.0
    assert record["added_risk"] == 1.0
    assert record["compensating_controls"] == ["benchmark_test",
                                               "complexity_waiver"]
    assert record["required_evidence"] == record["compensating_controls"]
    assert record["scope"] == ["src/parser.py"]
    assert "90d" in record["expires_at"]  # 默认过期复查


def test_exception_write_read(tmp_path, monkeypatch):
    monkeypatch.setattr(learn, "EXCEPTION_DIR", tmp_path)
    record = learn.exception_record("R-1", "reason", 2, 1, ["t1"], ["a.py"])
    path = learn.write_exception(record)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "R-1" in text and "compensating_controls" in text


# ---------------------------------------------------------------------------
# P6: 影子模式
# ---------------------------------------------------------------------------

def _draft_with_triggers(tmp_path, rule_id, terms):
    draft = learn.rule_schema_template()
    draft["id"] = rule_id
    draft["trigger"]["operations"] = terms
    learn.DRAFT_DIR = tmp_path  # noqa: SLF001（测试注入目录）
    path = tmp_path / f"{rule_id}-20260101.yaml"
    from pbatch.config import yaml
    path.write_text(yaml.safe_dump(draft, allow_unicode=True), encoding="utf-8")
    return path


def test_shadow_evaluate_hit_rate(tmp_path, monkeypatch):
    rule_id = "SHADOW-001"
    _draft_with_triggers(tmp_path, rule_id, ["重复记录", "并发"])
    log_path = tmp_path / "run.log"
    log_path.write_text("并发下单创建了两笔重复记录\n普通请求\n"
                        "并发场景重复写入\n正常日志\n", encoding="utf-8")
    monkeypatch.setattr(learn, "DRAFT_DIR", tmp_path)
    shadow = learn.shadow_evaluate(rule_id, str(log_path), draft_dir=tmp_path)
    assert shadow["hits"] == 2
    assert shadow["sample_lines"] == 4
    assert shadow["hit_rate"] == 0.5
    assert shadow["verdict"] == "promote_to_soft_policy"
    assert "治理流程" in shadow["note"]  # 硬规则升级仍须治理


def test_shadow_low_hit_rate_verdict(tmp_path, monkeypatch):
    rule_id = "SHADOW-002"
    _draft_with_triggers(tmp_path, rule_id, ["罕见信号"])
    log_path = tmp_path / "run.log"
    log_path.write_text("普通请求\n普通响应\n", encoding="utf-8")
    monkeypatch.setattr(learn, "DRAFT_DIR", tmp_path)
    shadow = learn.shadow_evaluate(rule_id, str(log_path), draft_dir=tmp_path)
    assert shadow["verdict"] == "insufficient_signal"


def test_shadow_missing_draft(tmp_path):
    shadow = learn.shadow_evaluate("NO-SUCH", str(tmp_path / "x.log"),
                                   draft_dir=tmp_path)
    assert "error" in shadow


def test_shadow_empty_triggers(tmp_path):
    rule_id = "SHADOW-003"
    _draft_with_triggers(tmp_path, rule_id, [])
    shadow = learn.shadow_evaluate(rule_id, str(tmp_path / "x.log"),
                                   draft_dir=tmp_path)
    assert "error" in shadow
