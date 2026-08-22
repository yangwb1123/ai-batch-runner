"""T14: difficulty-aware model routing + budget-aware downgrade tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pbatch import config, metering, routing
from pbatch.routing import TIER_ORDER, route_task, route_main, routing_enabled


def _profile(mode: int = 2, level: str = "L2_standard", load: float = 0.6,
             risk: str = "medium") -> dict:
    return {"mode": mode, "load": load, "workflow": {"level": level,
                                                     "risk": risk},
            "hard_triggers": []}


def test_difficulty_tier_mapping():
    assert routing.difficulty_tier(_profile(1, "L1_standard", 0.2)) == "low"
    assert routing.difficulty_tier(_profile(2, "L2_standard", 0.5)) == "medium"
    assert routing.difficulty_tier(_profile(2, "L2_standard", 0.7)) == "medium"
    assert routing.difficulty_tier(_profile(3, "L3_platform")) == "high"
    critical = _profile(3, "L3_platform", risk="high")
    critical["hard_triggers"] = [{"min_mode": 3, "matched": ["支付"]}]
    assert routing.difficulty_tier(critical) == "critical"


def test_route_uses_tier_model_from_config(monkeypatch):
    monkeypatch.setattr(routing, "_routing_config", lambda: {
        "enabled": True,
        "tiers": {"low": {"model": "cheap"}, "medium": {"model": "mid"},
                  "high": {"model": "big"}, "critical": {"model": "huge"}},
        "downgrade": {"enabled": True, "remaining_ratio_threshold": 0.25,
                      "min_tier": "low"},
    })
    plan = route_task("随便一查", profile=_profile(1, "L1_standard", 0.1))
    assert plan.tier == "low" and plan.model == "cheap"


def test_budget_downgrade(monkeypatch):
    monkeypatch.setattr(routing, "_routing_config", lambda: {
        "enabled": True,
        "tiers": {"low": {"model": "cheap"}, "medium": {"model": "mid"},
                  "high": {"model": "big"}, "critical": {"model": "huge"}},
        "downgrade": {"enabled": True, "remaining_ratio_threshold": 0.25,
                      "min_tier": "low"},
    })
    monkeypatch.setattr(metering, "DAILY_BUDGET", 100)
    monkeypatch.setattr(metering, "budget_spent_today", lambda: 90)  # 10% left
    plan = route_task("支付链路修复", profile=_profile(3, "L3_platform",
                                                       risk="high"))
    assert plan.downgraded and plan.remaining_ratio == pytest.approx(0.1)
    # tier 是最终执行档：降级后落到底线档并使用其模型
    assert plan.tier == "low" and plan.model == "cheap"
    assert any("downgraded" in r for r in plan.reasons)


def test_no_remaining_no_downgrade(monkeypatch):
    monkeypatch.setattr(routing, "_routing_config", lambda: {
        "enabled": True,
        "tiers": {"high": {"model": "big"}},
        "downgrade": {"enabled": True, "remaining_ratio_threshold": 0.25,
                      "min_tier": "low"},
    })
    monkeypatch.setattr(metering, "DAILY_BUDGET", 0)  # unlimited
    plan = route_task("x", profile=_profile(3, "L3_platform"))
    assert not plan.downgraded and plan.remaining_ratio is None


def test_cli_route_json(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "pbatch", "route", "查一下这个配置怎么改",
         "--json"], capture_output=True, text=True, timeout=60,
        cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["tier"] in TIER_ORDER
    assert "task" in data


def test_inject_only_untouched_models(monkeypatch):
    from pbatch import cli_batch
    from pbatch.models import Task

    calls = []

    def fake_route(prompt):
        calls.append(prompt)
        return routing.RoutingPlan(tier="medium", model="routed-model",
                                   reasons=["test"])
    # _inject_route_models 在函数内 from .routing import … → patch 源模块
    monkeypatch.setattr(routing, "route_task", fake_route)
    monkeypatch.setattr(routing, "routing_enabled", lambda: True)
    explicit = Task(prompt="e", model="explicit-model")
    blank = Task(prompt="b")
    cli_batch._inject_route_models([explicit, blank])
    assert explicit.model == "explicit-model"  # never overridden
    assert blank.model == "routed-model"
    assert calls == ["b"]

def test_plan_carries_thinking_from_tier(monkeypatch):
    monkeypatch.setattr(routing, "_routing_config", lambda: {
        "enabled": True, "default_thinking": "max",
        "tiers": {"low": {"model": "flash"},
                  "high": {"model": "big", "thinking": "medium"}},
        "downgrade": {"enabled": True, "remaining_ratio_threshold": 0.25,
                      "min_tier": "low"},
    })
    low = route_task("x", profile=_profile(1, "L1_standard", 0.1))
    assert low.thinking == "max"  # inherits default_thinking
    high = route_task("y", profile=_profile(3, "L3_platform"))
    assert high.thinking == "medium"  # tier-level wins


def test_inject_thinking_only_when_unset(monkeypatch):
    from pbatch import cli_batch
    from pbatch.models import Task
    monkeypatch.setattr(routing, "route_task", lambda prompt: routing.RoutingPlan(
        tier="low", model="flash", thinking="max", reasons=[]))
    monkeypatch.setattr(routing, "routing_enabled", lambda: True)
    set_task = Task(prompt="s", thinking="medium")
    blank = Task(prompt="b")
    cli_batch._inject_route_models([set_task, blank])
    assert set_task.thinking == "medium"  # explicit thinking never overridden
    assert blank.thinking == "max" and blank.model == "flash"
