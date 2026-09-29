"""难度感知的模型路由与预算感知降级（T14）。

`pi-batch route "任务"` 用零 LLM 成本的确定性画像（pbatch/profile.py）
把需求映射到难度档（low/medium/high/critical），再从 pi-batch.yaml
`model_routing.tiers` 查该档的推荐 model；结合每日 invocation 配额
（metering.daily budget）剩余比例做降级：剩余低于阈值时降至配置的
`min_tier`，避免高端模型在额度见底时被浪费。

纪律：
- 默认关闭（model_routing.enabled: false）——显式开启才介入；
- 路由只补缺省：任务/阶段显式指定的 model 永不覆盖；
- 降级必须可解释：RoutingPlan 携带 reason 字段，CLI 以 --json 输出；
- 预算判断是成本控制而非安全边界（与 metering 同款语义）。
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from typing import Optional

from . import config, metering, profile as profile_mod
from .config import log

TIER_ORDER = ("low", "medium", "high", "critical")
TIER_RANK = {tier: index for index, tier in enumerate(TIER_ORDER)}


@dataclass
class RoutingPlan:
    """One routing decision: tier, model, thinking, budget state, reasons."""

    tier: str
    model: str
    thinking: str = ""  # reasoning effort for the tier ("" = agent default)
    remaining_ratio: Optional[float] = None
    downgraded: bool = False
    reasons: list[str] = field(default_factory=list)
    profile_mode: int = 0
    source: str = "config"
    requested_tier: str = ""  # tier before budget-aware downgrade

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "requested_tier": self.requested_tier or self.tier,
            "model": self.model,
            "thinking": self.thinking,
            "remaining_ratio": self.remaining_ratio,
            "downgraded": self.downgraded,
            "reasons": self.reasons,
            "profile_mode": self.profile_mode,
            "source": self.source,
        }


def _routing_config() -> dict:
    section = config._section("model_routing")
    return section if isinstance(section, dict) else {}


def routing_enabled() -> bool:
    # Only a YAML boolean true opts in; strings like "false" must not enable routing.
    return _routing_config().get("enabled", False) is True


def _validate_workload(workload: int) -> None:
    if isinstance(workload, bool) or not isinstance(workload, int) or not 0 <= workload <= 3:
        raise ValueError("workload must be an integer from 0 through 3")


def difficulty_tier(profile: dict, workload: int = 0) -> str:
    """难度档（确定性启发式）：从 profile 画像映射。
    - workflow.level L3 / risk high → high
    - hard triggers（支付/生产/上线等）叠加 mode>=3 → critical
    - L2 / load >= 0.6 → medium；其余 → low。
    显式 workload 权重（0-3）可把中档推到高档。
    """
    _validate_workload(workload)
    workflow = profile.get("workflow") or {}
    level = str(workflow.get("level", ""))
    risk = str(workflow.get("risk", "")).lower()
    mode = int(profile.get("mode", 0) or 0)
    triggers = profile.get("hard_triggers") or []
    critical_hint = any(
        str(t.get("min_mode", 0)) == "3" or mode >= 3
        for t in triggers if isinstance(t, dict))
    if (level.startswith("L3") or risk == "high") and critical_hint:
        return "critical"
    if level.startswith("L3") or (risk == "high" and workload >= 2):
        return "high"
    if (level.startswith("L2") or float(profile.get("load", 0) or 0) >= 0.6
            or workload >= 1):
        return "medium"
    return "low"


def _tiers() -> dict:
    """Validate and normalize tier model/thinking declarations."""
    section = _routing_config()
    raw = section.get("tiers", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("model_routing.tiers must be a mapping")
    unknown = set(raw) - set(TIER_ORDER)
    if unknown:
        raise ValueError("unknown model_routing tier(s): "
                         + ", ".join(sorted(map(str, unknown))))
    default_thinking = section.get("default_thinking", "")
    if default_thinking is None:
        default_thinking = ""
    if not isinstance(default_thinking, str):
        raise ValueError("model_routing.default_thinking must be a string")
    tiers = {}
    for name in TIER_ORDER:
        entry = raw.get(name)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"model_routing.tiers.{name} must be a mapping")
        model = entry.get("model", "")
        thinking = entry.get("thinking", "")
        if model is None:
            model = ""
        if thinking is None:
            thinking = ""
        if not isinstance(model, str) or not isinstance(thinking, str):
            raise ValueError(f"model_routing.tiers.{name} model/thinking "
                             "must be strings")
        tiers[name] = {"model": model,
                       "thinking": thinking or default_thinking}
    return tiers


def _downgrade_policy() -> tuple[bool, float, str]:
    raw = _routing_config().get("downgrade", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("model_routing.downgrade must be a mapping")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("model_routing.downgrade.enabled must be boolean")
    if not enabled:
        return False, 0.0, "low"
    threshold = raw.get("remaining_ratio_threshold", 0.25)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("model_routing.downgrade.remaining_ratio_threshold "
                         "must be a finite number from 0 through 1")
    try:
        threshold = float(threshold)
    except (OverflowError, ValueError) as exc:
        raise ValueError("model_routing.downgrade.remaining_ratio_threshold "
                         "must be a finite number from 0 through 1") from exc
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("model_routing.downgrade.remaining_ratio_threshold "
                         "must be a finite number from 0 through 1")
    min_tier = raw.get("min_tier", "low")
    if not isinstance(min_tier, str) or min_tier not in TIER_RANK:
        raise ValueError("model_routing.downgrade.min_tier must be one of "
                         + ", ".join(TIER_ORDER))
    return True, float(threshold), min_tier


def _remaining_ratio() -> Optional[float]:
    """今日 invocation 配额剩余比例；无限额 → None。"""
    if metering.DAILY_BUDGET <= 0:
        return None
    spent = metering.budget_spent_today()
    return max(0.0, 1.0 - spent / metering.DAILY_BUDGET)


def route_task(desc: str, *, profile: Optional[dict] = None,
               workload: int = 0) -> RoutingPlan:
    """路由一个任务：画像 → 档位 → 模型（含预算降级）。零 LLM 成本。"""
    reasons: list[str] = []
    p = profile if profile is not None else profile_mod.task_profile(desc)
    tier = difficulty_tier(p, workload)
    reasons.append(f"profile mode={p.get('mode', 0)} "
                   f"workflow={p.get('workflow', {}).get('level', '-')}")
    tiers = _tiers()
    tier_entry = tiers.get(tier, {})
    model = str(tier_entry.get("model", ""))
    thinking = str(tier_entry.get("thinking", ""))
    if not model:
        model = config.AGENT_DEFAULT_MODEL or ""
    requested_tier = tier
    remaining = _remaining_ratio()
    downgraded = False
    downgrade_enabled, threshold, min_tier = _downgrade_policy()
    if downgrade_enabled and remaining is not None and remaining < threshold:
        if TIER_RANK[tier] > TIER_RANK[min_tier]:
            fallback = tiers.get(min_tier, {})
            fallback_model = str(fallback.get("model", ""))
            if not fallback_model:
                reasons.append(
                    f"daily quota remaining {remaining:.0%} < {threshold:.0%}, "
                    f"but no model is configured for min_tier '{min_tier}'; "
                    "kept the requested route")
            else:
                downgraded = True
                tier = min_tier
                model = fallback_model
                thinking = str(fallback.get("thinking", "")) or thinking
                reasons.append(
                    f"daily quota remaining {remaining:.0%} < {threshold:.0%}: "
                    f"downgraded to min_tier '{tier}'")
    return RoutingPlan(tier=tier, model=model, requested_tier=requested_tier,
                       thinking=thinking, remaining_ratio=remaining,
                       downgraded=downgraded,
                       reasons=reasons,
                       profile_mode=int(p.get("mode", 0) or 0))


def _nonnegative_budget(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("daily budget must be a nonnegative integer") from exc
    if not 0 <= number <= 2**63 - 1:
        raise argparse.ArgumentTypeError("daily budget must be a nonnegative 64-bit integer")
    return number


def _workload_value(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workload must be an integer from 0 through 3") from exc
    if not 0 <= number <= 3:
        raise argparse.ArgumentTypeError("workload must be an integer from 0 through 3")
    return number


def _cli_route_plan(desc: str, workload: int, daily_budget: int,
                    daily_state: str) -> RoutingPlan:
    previous_budget, previous_state = metering.DAILY_BUDGET, metering._DAILY_STATE
    metering.DAILY_BUDGET = daily_budget
    metering._DAILY_STATE = daily_state or str(config.data_dir() / "daily-budget.json")
    try:
        return route_task(desc, workload=workload)
    finally:
        metering.DAILY_BUDGET, metering._DAILY_STATE = previous_budget, previous_state


def route_main(argv: Optional[list[str]] = None) -> int:
    """pi-batch route "任务描述" [--json] [--workload N] [--daily-budget N]"""
    parser = argparse.ArgumentParser(
        prog="pi-batch route",
        description="难度感知模型路由：画像 → 难度档 → 推荐 model（预算感知降级）")
    parser.add_argument("task", nargs="+", help="任务描述")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--workload", type=_workload_value, default=0,
                        help="显式工作量权重 0-3（叠加到难度档）")
    parser.add_argument("--daily-budget", "--daily-invocations",
                        type=_nonnegative_budget, default=0,
                        help="每日 invocation 配额（0=无限；匹配执行 CLI 的 --daily-budget）")
    parser.add_argument("--daily-state", default="",
                        help="配额计数文件（默认使用当前 workspace 的 private runtime 文件）")
    args = parser.parse_args(argv)
    desc = " ".join(args.task)
    try:
        plan = _cli_route_plan(desc, args.workload, args.daily_budget,
                               args.daily_state)
    except ValueError as exc:
        parser.error(str(exc))
    if not routing_enabled():
        plan.reasons.insert(0, "model_routing.enabled=false — 仅供规划预览，未生效")
    if args.json:
        payload = plan.to_dict()
        payload["task"] = desc[:200]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    suffix = (f"  (requested {plan.requested_tier}; DOWNGRADED)"
              if plan.downgraded else "")
    print(f"tier     {plan.tier}{suffix}")
    print(f"model    {plan.model or '(agent default)'}")
    if plan.thinking:
        print(f"thinking {plan.thinking}")
    if plan.remaining_ratio is not None:
        print(f"quota    {plan.remaining_ratio:.0%} remaining today")
    for reason in plan.reasons:
        print(f"  - {reason}")
    return 0