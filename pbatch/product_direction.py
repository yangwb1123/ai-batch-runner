"""Deterministic product-direction brief generation.

``product_manifest`` answers *how much* product thinking a requirement needs.
This module turns that decision into a small, reviewable product artifact.  It
does not invent business facts: the target user, value, acceptance criteria,
and scenario decisions stay explicitly marked ``待确认`` until a requester
confirms them.  L0 requirements deliberately return only the restraint note
from the product manifest and do not acquire product structure.
"""

from __future__ import annotations

from typing import Optional

from . import config
from .product import LEVELS, product_manifest

_L0 = "L0_local_feature"
_BRIEF_LIMIT = 240
_PORTABLE_MANIFEST_FIELDS = (
    "level", "evidence", "scenarios", "specs", "restraint_notes",
)


def _normalize_requirement(text: str) -> str:
    """Validate and normalize one untrusted product requirement."""
    if not isinstance(text, str):
        raise ValueError("product requirement must be text")
    if len(text.encode("utf-8")) > config.INPUT_MAX_BYTES:
        raise ValueError(
            f"product requirement exceeds {config.INPUT_MAX_BYTES} bytes")
    value = " ".join(text.split())
    if not value:
        raise ValueError("product requirement must not be empty")
    return value


def _compact(value: str, limit: int = _BRIEF_LIMIT) -> str:
    """Keep the displayed requirement concise while classifying all input."""
    return value[:limit] + ("…" if len(value) > limit else "")


def _portable_manifest(manifest: dict) -> dict:
    """Drop runtime-only fields such as the local tool installation path."""
    result = {key: manifest.get(key) for key in _PORTABLE_MANIFEST_FIELDS}
    result["evidence"] = list(result.get("evidence") or [])
    result["specs"] = list(result.get("specs") or [])
    result["restraint_notes"] = list(result.get("restraint_notes") or [])
    result["scenarios"] = [
        {"scenario": item.get("scenario", ""),
         "questions": list(item.get("questions") or [])}
        for item in (result.get("scenarios") or [])
    ]
    return result


def _questions() -> list[dict]:
    """Build foundational questions not repeated by scenario rules."""
    questions = [
        "目标用户/角色是谁，操作边界是什么？",
        "用户要完成的目标及可观察价值是什么？",
        "主流程的开始、完成和验收条件是什么？",
        "失败、取消、重试或回滚时，用户应如何恢复？",
    ]
    return [{"question": question, "status": "待确认"}
            for question in questions]


def _journey() -> list[dict]:
    """Return a generic candidate journey; every business detail is pending."""
    return [
        {"id": "entry", "name": "进入", "goal": "到达功能入口",
         "status": "待确认"},
        {"id": "primary_action", "name": "主操作", "goal": "完成核心操作",
         "status": "待确认"},
        {"id": "result", "name": "结果", "goal": "看到可验证的结果",
         "status": "待确认"},
        {"id": "recovery", "name": "异常恢复", "goal": "失败后重试、取消或回滚",
         "status": "待确认"},
    ]


def _state_machine() -> dict:
    """Return candidate states that include an explicit recoverable failure."""
    states = [
        {"id": "not_started", "label": "未开始"},
        {"id": "in_progress", "label": "进行中"},
        {"id": "completed", "label": "已完成"},
        {"id": "recoverable_failure", "label": "失败（可恢复）"},
    ]
    transitions = [
        {"from": "not_started", "to": "in_progress", "event": "开始主流程"},
        {"from": "in_progress", "to": "completed", "event": "满足完成条件"},
        {"from": "in_progress", "to": "recoverable_failure", "event": "发生可恢复失败"},
        {"from": "recoverable_failure", "to": "in_progress", "event": "重试或恢复"},
    ]
    return {"status": "候选，待确认", "states": states,
            "transitions": transitions}


def _story(story_id: str, title: str, acceptance: str) -> dict:
    """Create a story with an honest, pending acceptance criterion."""
    return {"id": story_id, "title": title, "acceptance": acceptance,
            "status": "提案", "tasks": [{
                "id": f"{story_id}-TASK-1",
                "title": "补齐依赖、边界与验收证据",
                "status": "待确认",
            }]}


def _decomposition(level: str) -> dict:
    """Create the minimum Epic → Feature → Story → Task proposal for L1+."""
    features = [{
        "id": "FEATURE-1",
        "title": "核心用户路径",
        "stories": [
            _story("STORY-1", "用户完成核心目标", "待确认：Given/When/Then 验收条件"),
            _story("STORY-2", "用户看到成功结果", "待确认：结果字段、时效与可见范围"),
        ],
    }, {
        "id": "FEATURE-2",
        "title": "异常与恢复路径",
        "stories": [
            _story("STORY-3", "用户能识别并恢复失败", "待确认：失败提示、重试、取消或回滚"),
        ],
    }]
    if level in ("L2_platform_capability", "L3_product_feature"):
        features.append({
            "id": "FEATURE-3",
            "title": "边界与可追溯性",
            "stories": [
                _story("STORY-4", "操作遵守权限与组织边界", "待确认：角色、租户与数据隔离"),
                _story("STORY-5", "关键变更可审计和定位", "待确认：审计字段、事件与保留期"),
            ],
        })
    return {
        "status": "提案，待确认",
        "epic": {"id": "EPIC-1", "title": "核心产品方向", "status": "提案"},
        "features": features,
    }


def _business_rules(manifest: dict, level: str) -> list[dict]:
    """Separate pending scenario decisions from mandatory product guardrails."""
    rules = []
    for scenario in manifest.get("scenarios", []):
        for question in scenario.get("questions", []):
            rules.append({"source": scenario["scenario"],
                          "rule": question, "status": "待确认",
                          "kind": "candidate"})
    if level in ("L2_platform_capability", "L3_product_feature"):
        rules.extend([
            {"source": "L2/L3 产品化基线",
             "rule": "主查询与唯一约束必须携带 tenant_id 或组织上下文",
             "status": "必须遵守", "kind": "guardrail"},
            {"source": "L2/L3 产品化基线",
             "rule": "关键事件必须携带组织上下文并保留审计字段",
             "status": "必须遵守", "kind": "guardrail"},
            {"source": "L2/L3 产品化基线",
             "rule": "对外 API 必须明确版本边界",
             "status": "必须遵守", "kind": "guardrail"},
        ])
    return rules


def product_brief(text: str, level: Optional[str] = None) -> dict:
    """Build a deterministic product-direction artifact from requirement text.

    The artifact is intentionally a proposal.  It is safe to feed into a
    review or planning stage because unknown business decisions are questions,
    not implementation instructions.
    """
    if level is not None and level not in LEVELS:
        raise ValueError("level must be one of: " + ", ".join(LEVELS))
    source = _normalize_requirement(text)
    requirement = _compact(source)
    # Detect signals from the complete bounded input; only the displayed
    # requirement field is shortened for report readability.
    raw_manifest = product_manifest(source, level)
    manifest = _portable_manifest(raw_manifest)
    resolved = manifest["level"]
    base = {
        "schema_version": "product-brief/v1",
        "requirement": requirement,
        "productization": manifest,
        "status": "不适用" if resolved == _L0 else "提案，待确认",
    }
    if resolved == _L0:
        notes = manifest.get("restraint_notes") or []
        reason = (notes[0] if notes else
                  "L0 小工具遵守克制原则，不生成产品结构")
        return {**base, "direction": {"status": "不适用",
                "reason": reason}, "user_story": {"status": "不适用"},
                "confirmation_questions": [], "journey": [],
                "state_machine": {"status": "不适用"},
                "decomposition": {"status": "不适用"},
                "business_rules": []}
    direction = {
        "status": "待确认",
        "target_user": "待确认",
        "problem": f"待确认：围绕“{requirement}”要解决的用户问题",
        "value": "待确认",
        "success_signal": "待确认",
    }
    return {**base, "direction": direction,
            "user_story": {"as_a": "待确认的目标用户",
                           "i_want": requirement,
                           "so_that": "待确认的业务价值",
                           "status": "待确认"},
            "journey": _journey(), "state_machine": _state_machine(),
            "decomposition": _decomposition(resolved),
            "business_rules": _business_rules(manifest, resolved),
            "confirmation_questions": _questions()}


def _lines_for_features(features: list[dict]) -> list[str]:
    """Render feature/story rows for the Markdown formatter."""
    lines = []
    for feature in features:
        lines.append(f"- {feature['id']}：{feature['title']}")
        for story in feature.get("stories", []):
            lines.append(f"  - {story['id']}：{story['title']}（{story['acceptance']}）")
            for task in story.get("tasks", []):
                lines.append(f"    - {task['id']}：{task['title']}（{task['status']}）")
    return lines


def _state_lines(state_machine: dict) -> list[str]:
    """Render state transitions with user-facing labels instead of ids."""
    states = state_machine.get("states", [])
    labels = {item["id"]: item["label"] for item in states}
    lines = [f"- {state['label']}" for state in states]
    for item in state_machine.get("transitions", []):
        start = labels.get(item["from"], item["from"])
        end = labels.get(item["to"], item["to"])
        lines.append(f"- {start} --{item['event']}→ {end}（待确认）")
    return lines


def _unique_lines(values: list[str]) -> list[str]:
    """Keep report sections deterministic and free of duplicate prose."""
    return list(dict.fromkeys(value for value in values if value))


def format_product_brief(brief: dict) -> str:
    """Render a product brief as a concise Markdown review artifact."""
    product = brief["productization"]
    lines = ["# 产品方向简报", "", f"- 需求：{brief['requirement']}",
             f"- 产品化分级：{product['level']}", f"- 状态：{brief['status']}"]
    direction = brief["direction"]
    if direction["status"] == "不适用":
        notes = _unique_lines(
            [direction.get("reason", ""), *product.get("restraint_notes", [])])
        lines += ["", "## 克制边界"]
        lines += [f"- {note}" for note in notes]
        return "\n".join(lines)
    lines += ["", "## 产品方向", f"- 目标用户：{direction['target_user']}",
              f"- 要解决的问题：{direction['problem']}",
              f"- 用户价值：{direction['value']}",
              f"- 成功信号：{direction['success_signal']}", "", "## 用户故事",
              f"- As a：{brief['user_story']['as_a']}",
              f"- I want：{brief['user_story']['i_want']}",
              f"- So that：{brief['user_story']['so_that']}"]
    lines += ["", "## 用户旅程"]
    lines += [f"- {item['name']}：{item['goal']}（{item['status']}）"
              for item in brief["journey"]]
    lines += ["", "## 状态机（候选）"]
    lines += _state_lines(brief["state_machine"])
    lines += ["", "## 功能分解（提案）"]
    lines += [f"- {brief['decomposition']['epic']['id']}："
              f"{brief['decomposition']['epic']['title']}"]
    lines += _lines_for_features(brief["decomposition"]["features"])
    if brief["business_rules"]:
        lines += ["", "## 业务规则"]
        lines += [f"- [{item['status']}] {item['rule']}"
                  for item in brief["business_rules"]]
    lines += ["", "## 待确认问题"]
    lines += [f"- [{item['status']}] {item['question']}"
              for item in brief["confirmation_questions"]]
    notes = _unique_lines(product.get("restraint_notes", []))
    if notes:
        lines += ["", "## 克制边界"]
        lines += [f"- {note}" for note in notes]
    return "\n".join(lines)
