"""T14: difficulty-aware model routing + budget-aware downgrade tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
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
    profile = _profile(3, "L3_platform", risk="high")
    profile["hard_triggers"] = [{"min_mode": 3}]
    plan = route_task("支付链路修复", profile=profile)
    assert plan.downgraded and plan.remaining_ratio == pytest.approx(0.1)
    # min_tier 是预算触发后的目标档；critical 可直接降到 low floor。
    assert plan.tier == "low" and plan.model == "cheap"
    assert plan.requested_tier == "critical"
    assert any("downgraded" in r for r in plan.reasons)


def test_missing_min_tier_model_does_not_claim_budget_downgrade(monkeypatch):
    monkeypatch.setattr(routing, "_routing_config", lambda: {
        "enabled": True, "tiers": {"critical": {"model": "huge"}},
        "downgrade": {"enabled": True, "remaining_ratio_threshold": 0.25,
                      "min_tier": "low"},
    })
    monkeypatch.setattr(metering, "DAILY_BUDGET", 100)
    monkeypatch.setattr(metering, "budget_spent_today", lambda: 90)
    profile = _profile(3, "L3_platform", risk="high")
    profile["hard_triggers"] = [{"min_mode": 3}]

    plan = route_task("支付链路修复", profile=profile)

    assert plan.tier == "critical" and plan.model == "huge"
    assert not plan.downgraded
    assert plan.requested_tier == "critical"
    assert any("no model is configured" in reason for reason in plan.reasons)


def test_invalid_downgrade_policy_fails_closed(monkeypatch):
    monkeypatch.setattr(routing, "_routing_config", lambda: {
        "enabled": True, "tiers": {"high": {"model": "big"}},
        "downgrade": {"enabled": True, "remaining_ratio_threshold": 1.5,
                      "min_tier": "unknown"},
    })
    with pytest.raises(ValueError, match="remaining_ratio_threshold"):
        route_task("task", profile=_profile(3, "L3_platform"))


def test_unknown_min_tier_is_rejected(monkeypatch):
    monkeypatch.setattr(routing, "_routing_config", lambda: {
        "tiers": {"high": {"model": "big"}},
        "downgrade": {"enabled": True, "remaining_ratio_threshold": 0.25,
                      "min_tier": "premium"},
    })
    with pytest.raises(ValueError, match="min_tier"):
        route_task("task", profile=_profile(3, "L3_platform"))


def test_malformed_tier_config_is_rejected(monkeypatch):
    monkeypatch.setattr(routing, "_routing_config", lambda: {
        "tiers": {"premuim": {"model": "typo-model"}},
    })
    with pytest.raises(ValueError, match="unknown model_routing tier"):
        route_task("task", profile=_profile())


def test_non_boolean_enabled_does_not_opt_in(monkeypatch):
    monkeypatch.setattr(routing, "_routing_config", lambda: {"enabled": "true"})
    assert not routing.routing_enabled()


def test_workload_is_bounded_to_documented_range():
    with pytest.raises(ValueError, match="0 through 3"):
        routing.difficulty_tier(_profile(), workload=4)
    with pytest.raises(ValueError, match="0 through 3"):
        route_task("task", profile=_profile(), workload=-1)


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
    repo_root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        repo_root, env.get("PYTHONPATH", ""))))
    result = subprocess.run(
        [sys.executable, "-m", "pbatch", "route", "查一下这个配置怎么改",
         "--json"], capture_output=True, text=True, timeout=60,
        cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["tier"] in TIER_ORDER
    assert "task" in data


def test_route_cli_rejects_out_of_range_workload():
    with pytest.raises(SystemExit):
        route_main(["task", "--workload", "4"])


def test_route_preview_uses_execution_budget_and_state(
        tmp_path, monkeypatch, capsys):
    profile = _profile(3, "L3_platform", risk="high")
    profile["hard_triggers"] = [{"min_mode": 3}]
    monkeypatch.setattr(routing, "_routing_config", lambda: {
        "enabled": True,
        "tiers": {"low": {"model": "cheap"},
                  "critical": {"model": "huge"}},
        "downgrade": {"enabled": True, "remaining_ratio_threshold": 0.25,
                      "min_tier": "low"},
    })
    monkeypatch.setattr(routing.profile_mod, "task_profile", lambda _task: profile)
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (tmp_path / "daily-budget.json").write_text(
        json.dumps({"date": today, "count": 90}), encoding="utf-8")

    assert route_main(["支付生产故障", "--daily-budget", "100", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["tier"] == "low"
    assert result["remaining_ratio"] == pytest.approx(0.1)
    assert result["downgraded"] is True


def test_task_default_model_is_resolved_when_constructed(monkeypatch):
    from pbatch.models import Task
    monkeypatch.setattr(config, "AGENT_DEFAULT_MODEL", "late-model")
    assert Task(prompt="dynamic").model == "late-model"


def test_pipeline_from_prompt_uses_current_default_model(monkeypatch, tmp_path):
    from pbatch.models import Stage
    from pbatch.pipeline import _stage_from_prompt_tasks
    monkeypatch.setattr(config, "AGENT_DEFAULT_MODEL", "pipeline-model")
    tasks, reused = _stage_from_prompt_tasks(
        Stage(name="stage", from_prompt="prompt", output=str(tmp_path / "out.md")),
        False, "", 0)
    assert not reused and tasks[0].model == "pipeline-model"


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
    explicit_default = Task(
        prompt="ed", model=config.AGENT_DEFAULT_MODEL,
        memory={"model_route_source": "task_model"})
    blank = Task(prompt="b")
    cli_batch._inject_route_models([explicit, explicit_default, blank])
    assert explicit.model == "explicit-model"  # never overridden
    assert explicit_default.model == config.AGENT_DEFAULT_MODEL
    assert blank.model == "routed-model"
    assert blank.memory["model_route_source"] == "difficulty"
    assert blank.memory["model_route_tier"] == "medium"
    assert blank.memory["model_route_requested_tier"] == "medium"
    assert blank.memory["model_route_downgraded"] is False
    assert calls == ["b"]

def test_task_loading_records_model_selection_provenance(tmp_path):
    from pbatch.task_loading import _tasks_from_data, task_from_prompt

    defaulted = task_from_prompt("prompt", str(tmp_path))
    explicit = _tasks_from_data(
        {"tasks": [{"prompt": "prompt", "model": "fixed-model"}]},
        str(tmp_path), "tasks.yaml")[0]

    assert defaulted.memory["model_route_source"] == "agent_default"
    assert explicit.memory["model_route_source"] == "task_model"


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
