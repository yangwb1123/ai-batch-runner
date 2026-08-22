"""难度感知的模型路由与预算感知降级（T14）。

`pi-batch route "任务"` 用零 LLM 成本的确定性画像（pbatch/profile.py）
把需求映射到难度档（low/medium/high/critical），再从 pi-batch.yaml
`model_routing.tiers` 查该档的推荐 model；结合每日 token 配额
（metering.daily budget）剩余比例做降级：剩余低于阈值时最高可用档
下调一级，避免高端模型在额度见底时被浪费。

纪律：
- 默认关闭（model_routing.enabled: false）——显式开启才介入；
- 路由只补缺省：任务/阶段显式指定的 model 永不覆盖；
- 降级必须可解释：RoutingPlan 携带 reason 字段，CLI 以 --json 输出；
- 预算判断是成本控制而非安全边界（与 metering 同款语义）。
"""

from __future__ import annotations

import argparse
import json
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

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
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
    return bool(_routing_config().get("enabled", False))


def difficulty_tier(profile: dict, workload: int = 0) -> str:
    """难度档（确定性启发式）：从 profile 画像映射。
    - workflow.level L3 / risk high → high
    - hard triggers（支付/生产/上线等）叠加 mode>=3 → critical
    - L2 / load >= 0.6 → medium；其余 → low。
    显式 workload 权重（0-3）可把中档推到高档。
    """
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
    """tier -> {model, thinking}; thinking inherits the top-level
    default_thinking when the tier omits it (cost play: cheap model +
    max reasoning keeps quality close to a旗舰 model)."""
    raw = _routing_config().get("tiers") or {}
    default_thinking = str(_routing_config().get("default_thinking", "") or "")
    tiers: dict = {}
    for name in TIER_ORDER:
        entry = raw.get(name)
        if not isinstance(entry, dict):
            continue
        tiers[name] = {
            "model": str(entry.get("model", "") or ""),
            "thinking": str(entry.get("thinking", "") or default_thinking),
        }
    return tiers


def _remaining_ratio() -> Optional[float]:
    """今日 token/invocations 配额剩余比例；无限额 → None。"""
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
    remaining = _remaining_ratio()
    downgraded = False
    downgrade_cfg = _routing_config().get("downgrade") or {}
    threshold = float(downgrade_cfg.get("remaining_ratio_threshold", 0.25))
    min_tier = str(downgrade_cfg.get("min_tier", "low"))
    if downgrade_cfg.get("enabled", True) and remaining is not None:
        if remaining < threshold and TIER_RANK.get(tier, 0) > TIER_RANK.get(min_tier, 0):
            downgraded = True
            tier = min_tier
            fallback = tiers.get(tier, {})
            model = str(fallback.get("model", "")) or model
            thinking = str(fallback.get("thinking", "")) or thinking
            reasons.append(
                f"daily quota remaining {remaining:.0%} < {threshold:.0%}: "
                f"downgraded to '{tier}'")
    return RoutingPlan(tier=tier, model=model, thinking=thinking,
                       remaining_ratio=remaining, downgraded=downgraded,
                       reasons=reasons,
                       profile_mode=int(p.get("mode", 0) or 0))


def route_main(argv: Optional[list[str]] = None) -> int:
    """pi-batch route "任务描述" [--json] [--workload N]"""
    parser = argparse.ArgumentParser(
        prog="pi-batch route",
        description="难度感知模型路由：画像 → 难度档 → 推荐 model（预算感知降级）")
    parser.add_argument("task", nargs="+", help="任务描述")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--workload", type=int, default=0,
                        help="显式工作量权重 0-3（叠加到难度档）")
    args = parser.parse_args(argv)
    desc = " ".join(args.task)
    plan = route_task(desc, workload=args.workload)
    if not routing_enabled():
        plan.reasons.insert(0, "model_routing.enabled=false — 仅供规划预览，未生效")
    if args.json:
        payload = plan.to_dict()
        payload["task"] = desc[:200]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"tier     {plan.tier}" + ("  (DOWNGRADED)" if plan.downgraded else ""))
    print(f"model    {plan.model or '(agent default)'}")
    if plan.thinking:
        print(f"thinking {plan.thinking}")
    if plan.remaining_ratio is not None:
        print(f"quota    {plan.remaining_ratio:.0%} remaining today")
    for reason in plan.reasons:
        print(f"  - {reason}")
    return 0