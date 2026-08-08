"""Tests for 字典序决策 / 仲裁顺序 / 并行判定 / 加 Agent 判据 / 状态机 / 质量底线.

decision: 字典序分层（安全层一票否决）、仲裁顺序（工具 > 测试 > 自我）、
质量底线前置。
waves: parallel_safe 完整公式、add_agent_benefit、deadline 紧迫度。
states: 设备/尝试状态机非法迁移拒绝、LOST 副作用策略。
"""

from __future__ import annotations

import pytest

from pbatch import decision, states
from pbatch.decision import (LAYER_ORDER, SOURCE_ORDER, arbitrate,
                             quality_floor_met, select_lexicographic)
from pbatch.fabric_sched import TaskNode
from pbatch.states import (attempt_transition, device_transition,
                           lost_policy)
from pbatch.waves import add_agent_benefit, parallel_safe


def _node(nid, deps=(), wset=(), flexibility="select"):
    return TaskNode(id=nid, goal=f"g-{nid}", argv=["echo", nid],
                    depends_on=list(deps), write_set=list(wset),
                    flexibility=flexibility)


# ---------------------------------------------------------------------------
# 字典序决策
# ---------------------------------------------------------------------------

def test_lexicographic_safety_layer_vetoes():
    """低成本不能补偿错误结果：安全层差异一票定胜负。"""
    candidates = [
        {"id": "cheap-risky", "layers": {"safety": 0.2, "resources": 1.0}},
        {"id": "safe-slow", "layers": {"safety": 0.9, "resources": 0.3}},
    ]
    best = select_lexicographic(candidates)
    assert best["id"] == "safe-slow"


def test_lexicographic_breaks_ties_by_next_layer():
    candidates = [
        {"id": "a", "layers": {"safety": 0.9, "correctness": 0.5,
                               "user_experience": 0.8}},
        {"id": "b", "layers": {"safety": 0.9, "correctness": 0.9,
                               "user_experience": 0.1}},
    ]
    assert select_lexicographic(candidates)["id"] == "b"


def test_lexicographic_minimize_layer():
    candidates = [
        {"id": "cheap", "layers": {"safety": 0.9, "resources": 0.1}},
        {"id": "pricey", "layers": {"safety": 0.9, "resources": 0.9}},
    ]
    best = select_lexicographic(candidates, minimize={"resources"})
    assert best["id"] == "cheap"  # resources 越小越好 → 0.1 更优


def test_arbitrate_source_priority():
    """确定性工具输出 > 运行测试 > 自我评价（不能多数投票代替验证）。"""
    claims = [
        {"id": "c1", "source_type": "self", "verdict": "pass"},
        {"id": "c2", "source_type": "self", "verdict": "pass"},
        {"id": "c3", "source_type": "tool", "verdict": "fail"},
    ]
    result = arbitrate(claims)
    assert result["verdict"] == "fail"  # 工具证据优先，忽略 2:1 多数
    assert result["source"] == "tool"


def test_arbitrate_majority_within_layer():
    claims = [
        {"id": "r1", "source_type": "reviewer", "verdict": "pass"},
        {"id": "r2", "source_type": "reviewer", "verdict": "pass"},
        {"id": "r3", "source_type": "reviewer", "verdict": "fail"},
    ]
    assert arbitrate(claims)["verdict"] == "pass"


def test_arbitrate_empty():
    assert arbitrate([])["verdict"] is None


def test_quality_floor_before_resources():
    ok, missing = quality_floor_met({"acceptance": 1.0, "tests": True},
                                    {"acceptance": 1.0, "tests": True})
    assert ok and missing == []
    ok2, missing2 = quality_floor_met({"acceptance": 0.6, "tests": False},
                                      {"acceptance": 1.0, "tests": True})
    assert ok2 is False and missing2 == ["acceptance", "tests"]


def test_layer_and_source_order():
    assert LAYER_ORDER[0] == "safety"
    assert SOURCE_ORDER == ("formal", "tool", "test", "experiment",
                            "reviewer", "majority", "self")


# ---------------------------------------------------------------------------
# 并行判定 / 加 Agent 判据
# ---------------------------------------------------------------------------

def test_parallel_safe_independent_nodes():
    a = _node("a", wset=("x",))
    b = _node("b", wset=("y",))
    assert parallel_safe(a, b) is True


def test_parallel_safe_dependency_blocks():
    a = _node("a")
    b = _node("b", deps=("a",))
    assert parallel_safe(a, b) is False


def test_parallel_safe_write_conflict_blocks():
    a = _node("a", wset=("shared",))
    b = _node("b", wset=("shared",))
    assert parallel_safe(a, b) is False


def test_parallel_safe_fixed_nodes_serial():
    a = _node("a", flexibility="fixed")
    b = _node("b", flexibility="select")
    assert parallel_safe(a, b) is False  # fixed 必须按明确顺序


def test_add_agent_benefit_threshold():
    """收益为正才加 Agent；协调/合并成本高 → 不加。"""
    split = add_agent_benefit(specialization_gain=1.5,
                              critical_path_reduction=1.0,
                              coordination_cost=0.5,
                              context_duplication=0.5, merge_risk=0.5)
    assert split["should_add"] is True
    keep = add_agent_benefit(specialization_gain=0.2, coordination_cost=1.0,
                             context_duplication=1.0, merge_risk=1.0)
    assert keep["should_add"] is False
    assert "N 个项目" in keep["note"]


def test_priority_deadline_urgency():
    from pbatch.waves import priority_score
    nodes = [TaskNode(id="a", goal="g", argv=[], estimate_seconds=1.0),
             TaskNode(id="b", goal="g", argv=[], estimate_seconds=1.0)]
    score_a = priority_score(nodes[0], nodes, deadline_urgency=0.5)
    score_b = priority_score(nodes[1], nodes, deadline_urgency=0.0)
    assert score_a > score_b


# ---------------------------------------------------------------------------
# 设备/尝试状态机
# ---------------------------------------------------------------------------

def test_device_state_machine_lifecycle():
    state = "unregistered"
    state = device_transition(state, "enrolled")
    state = device_transition(state, "ready")
    state = device_transition(state, "busy")
    state = device_transition(state, "draining")
    state = device_transition(state, "offline")
    assert state == "offline"


def test_device_illegal_transition():
    with pytest.raises(ValueError):
        device_transition("ready", "enrolled")  # 已就绪不能回退注册
    with pytest.raises(ValueError):
        device_transition("unregistered", "busy")


def test_attempt_state_machine():
    state = "created"
    for step in ("placed", "reserved", "running", "verifying", "succeeded"):
        state = attempt_transition(state, step)
    assert state == "succeeded"
    with pytest.raises(ValueError):
        attempt_transition("created", "succeeded")


def test_lost_is_not_failed():
    """LOST：不知道实际结果——不可逆操作禁止自动重试。"""
    assert lost_policy("irreversible") == "暂停，等待状态确认（禁止自动重试）"
    assert "重新执行" in lost_policy("pure")
    assert "对账" in lost_policy("compensatable")
