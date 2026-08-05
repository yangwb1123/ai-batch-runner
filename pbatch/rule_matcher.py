"""Deterministic rule matching: which ui-spec rules apply to a task.

The spec system is large; a small demo must not load all of it. This
module is the algorithmic half of rule selection (the LLM stays inside
the emitted candidate set). A task is classified along three axes:

- scale:    demo < standard < production (default standard; demos must be
            marked with demo/示例/原型 keywords)
- page_type: form/table/detail/workbench/immersive/auth (matched keywords)
- risk:     high-risk business actions (pay/approve/delete/submit...)

`match_rules` returns a bounded manifest: rule id, tier, matched evidence,
and the spec files to load. Prompts inject ONLY the manifest; agents load
the files on demand (progressive disclosure, like the memory manifest).

Usage:
    pi-batch rules "生成一个支付表单页"            # human manifest
    pi-batch rules "登录页 demo" --json            # machine report
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from . import config
from .classifier import TaskClassification, classify_text
from .config import log, yaml
from .relevance import _keyword_hit
from .text_io import read_text_bounded

TIER_ORDER = {"demo": 0, "standard": 1, "production": 2}
DEFAULT_TIER = "standard"

_DEFAULT_REGISTRY = {
    "scale": {
        "demo": ["demo", "示例", "演示", "原型", "prototype", "小demo"],
        "production": ["生产", "上线", "正式", "企业", "production", "enterprise"],
    },
    "page_types": {
        "form": ["表单", "新建", "提交页", "form"],
        "table": ["表格", "列表", "分页", "批量", "table"],
        "detail": ["详情", "detail"],
        "workbench": ["工作台", "仪表盘", "看板", "dashboard"],
        "immersive": ["特效", "落地页", "官网", "营销", "动画"],
        "auth": ["登录", "注册", "login", "register"],
    },
    "risk": {"high": ["支付", "审批", "删除", "提交", "作废", "订单", "payment", "approve", "delete"]},
    "rules": {
        "visual-core": {"files": ["ui-specs/spacing.md", "ui-specs/anti-patterns.md"],
                        "min_tier": "demo", "required": True,
                        "description": "8pt 间距 token 与反例清单"},
        "component-spec": {"files": ["ui-specs/component-spec.md"], "min_tier": "demo",
                           "description": "组件尺寸/状态/行为规范"},
        "business-profile": {"files_template": "ui-specs/business-profiles/{profile}.md",
                             "min_tier": "demo", "description": "业务风格配置"},
    },
}


def _default_registry_paths() -> list:
    return [Path(__file__).resolve().parent.parent / "ui-specs" / "rules.yaml",
            Path("ui-specs/rules.yaml")]


def load_registry(path: str = "") -> dict:
    """Load ui-specs/rules.yaml; YAML sections extend the built-in defaults."""
    merged = {section: dict(value) for section, value in _DEFAULT_REGISTRY.items()}
    if not yaml:
        return merged
    candidates = [Path(path)] if path else _default_registry_paths()
    for candidate in candidates:
        try:
            data = yaml.safe_load(read_text_bounded(
                candidate, config.INPUT_MAX_BYTES, "rule registry")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for section, value in data.items():
            if section in merged and isinstance(value, dict):
                merged[section].update({str(k): v for k, v in value.items()
                                        if isinstance(v, (dict, list))})
    return merged


def _best_hits(text: str, section: dict, lowered: str = "") -> list:
    """All section entries with their matched keywords (evidence)."""
    hits = []
    for name, terms in section.items():
        matched = tuple(str(t) for t in terms if _keyword_hit(lowered, str(t)))
        if matched:
            hits.append((name, matched))
    return hits


def detect_scale(text: str, registry: dict) -> tuple:
    """(tier, evidence): demo/production keywords win over the default."""
    lowered = (text or "").lower()
    for tier in ("demo", "production"):
        matched = tuple(str(t) for t in registry.get("scale", {}).get(tier, [])
                        if _keyword_hit(lowered, str(t)))
        if matched:
            return tier, matched
    return DEFAULT_TIER, ()


def _rule_skip_reason(rule: dict, tier_rank: int, page_types: list,
                      high_risk: bool, profile: str) -> str:
    """Why a rule does NOT apply (empty string = it applies)."""
    min_rank = TIER_ORDER.get(rule.get("min_tier", "standard"), 1)
    if tier_rank < min_rank:
        return f"tier below required {rule.get('min_tier')}"
    if rule.get("page_types") and not (set(rule["page_types"]) & set(page_types)):
        return "page type not matched"
    if rule.get("profiles") and profile not in rule.get("profiles", []):
        return f"profile '{profile or 'unknown'}' not in {rule.get('profiles')}"
    if rule.get("required_when_risk") and not high_risk:
        return "not high-risk"
    return ""


def match_rules(text: str, classification: Optional[TaskClassification] = None,
                registry: Optional[dict] = None) -> dict:
    """Deterministic rule manifest for a task text.

    Returns {tier, page_types, risk, rules: [{id, tier, evidence, files,
    description, required}], skipped: [{id, reason}]}.
    """
    reg = registry or load_registry()
    lowered = (text or "").lower()
    tier, tier_evidence = detect_scale(text, reg)
    page_hits = _best_hits(lowered, reg.get("page_types", {}), lowered)
    risk_hits = _best_hits(lowered, reg.get("risk", {}), lowered)
    page_types = [name for name, _ in page_hits]
    high_risk = bool(risk_hits)
    tier_rank = TIER_ORDER.get(tier, TIER_ORDER[DEFAULT_TIER])

    classification = classification or classify_text(text)
    profile = getattr(classification, "profile", "") or ""

    rules, skipped = [], []
    for rule_id, rule in reg.get("rules", {}).items():
        reason = _rule_skip_reason(rule, tier_rank, page_types, high_risk, profile)
        if reason:
            skipped.append({"id": rule_id, "reason": reason})
            continue
        files = _resolve_files(rule, profile)
        rules.append({
            "id": rule_id, "tier": rule.get("min_tier", "standard"),
            "required": bool(rule.get("required", False)),
            "description": rule.get("description", ""),
            "files": files, "evidence": {
                "scale": list(tier_evidence), "page_types": page_types,
                "risk": "high" if high_risk else "low",
                "profile": profile,
            },
        })
    rules.sort(key=lambda item: (0 if item["required"] else 1, item["tier"]))
    return {"tier": tier, "page_types": page_types, "risk": "high" if high_risk else "low",
            "profile": profile, "rules": rules, "skipped": skipped}


def _resolve_files(rule: dict, profile: str) -> list:
    """Rule files; profiles rules resolve the {profile} template."""
    template = rule.get("files_template")
    if template and profile:
        return [template.replace("{profile}", profile)]
    return list(rule.get("files", []))


def format_manifest(matched: dict, limit: int = 8) -> str:
    """Human/markdown manifest for prompt injection (bounded)."""
    lines = [f"## Applicable UI rules (deterministic manifest, tier={matched['tier']}, "
             f"risk={matched['risk']}, profile={matched['profile'] or 'generic'})"]
    lines.append("Apply ONLY these rules; load the listed files, do not load unrelated specs:")
    rules = matched["rules"]
    shown = rules[:limit]
    for item in shown:
        marker = "REQUIRED" if item["required"] else "optional"
        lines.append(f"- [{marker}] {item['id']} ({item['tier']}): {item['description']}")
        for file in item["files"]:
            lines.append(f"    {file}")
    hidden = rules[limit:]
    if hidden:
        lines.append(f"- ... and {len(hidden)} more rule(s) in this manifest: "
                     + ", ".join(item["id"] for item in hidden))
    skipped = matched.get("skipped", [])
    if skipped:
        lines.append("Skipped: " + ", ".join(
            f"{item['id']} ({item['reason']})" for item in skipped))
    return "\n".join(lines)


def rules_main(argv: list) -> None:
    """`pi-batch rules "<task>" [--json]` — print the deterministic rule
    manifest for a task without executing anything."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="pi-batch.py rules",
        description="Match the applicable ui-spec rules to a task "
                    "(algorithmic; tier=scale × page_type × risk).")
    parser.add_argument("task", nargs="+", help="task prompt text")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--registry", default="", help="rule registry YAML override")
    args = parser.parse_args(argv)
    text = " ".join(args.task)
    matched = match_rules(text, registry=load_registry(args.registry))
    if args.json:
        print(json.dumps(matched, ensure_ascii=False, indent=2))
        return
    print(format_manifest(matched))
