"""Tests for 收尾批：进展度量 / 软规则权重 / flexibility / 图操作 / NFR / 模型缓存感知.

governance: progress_score 单调度量、软规则权重更新（η 公式）。
fabric_sched: TaskNode.flexibility 字段、flexibility_budget、模型缓存标签优先。
waves: 图操作 expand/prune/insert/reroute/merge。
profile: NFR 一等节点清单（风险/模式驱动）。
"""

from __future__ import annotations

import pytest

from pbatch import governance_policy as gp
from pbatch.fabric_sched import (TaskNode, flexibility_budget, pick_device,
                                 _model_cached)
from pbatch.profile import nfr_requirements, task_profile
from pbatch.waves import (expand_node, insert_node, merge_nodes, prune_nodes,
                          reroute_node)


def _node(nid, deps=(), wset=(), flexibility="select"):
    return TaskNode(id=nid, goal=f"g-{nid}", argv=["echo", nid],
                    depends_on=list(deps), write_set=list(wset),
                    flexibility=flexibility)


# ---------------------------------------------------------------------------
# 进展度量 + 软规则权重
# ---------------------------------------------------------------------------

def test_progress_score_formula():
    assert gp.progress_score(0.8, 3, 1, 0) == 2.8
    assert gp.progress_score(1.0, 5, 0, 0) == 6.0
    # 回归数把进展拉低（无实质进展信号）
    assert gp.progress_score(0.5, 1, 2, 3) == -3.5


def test_soft_rule_weight_update():
    assert gp.update_soft_rule_weight(0.5, 1.0, eta=0.5) == 0.75
    assert gp.update_soft_rule_weight(0.5, 0.0, eta=0.5) == 0.25
    assert gp.update_soft_rule_weight(0.5, 1.0, eta=0.0) == 0.5  # 不学习
    with pytest.raises(ValueError):
        gp.update_soft_rule_weight(0.5, 1.0, eta=1.5)


# ---------------------------------------------------------------------------
# TaskNode.flexibility + 预算
# ---------------------------------------------------------------------------

def test_flexibility_field_default():
    assert TaskNode(id="a", goal="g", argv=[]).flexibility == "select"
    fixed = TaskNode(id="b", goal="g", argv=[], flexibility="fixed")
    assert fixed.flexibility == "fixed"


def test_flexibility_budget():
    nodes = [_node("a", flexibility="fixed"),
             _node("b", flexibility="select"),
             _node("c", flexibility="generative"),
             _node("d", flexibility="generative")]
    budget = flexibility_budget(nodes)
    assert budget["counts"] == {"fixed": 1, "select": 1, "generative": 2}
    assert budget["generative_share"] == 0.5


# ---------------------------------------------------------------------------
# 图操作（AADM-R §8）
# ---------------------------------------------------------------------------

def test_expand_node():
    nodes = [_node("parent"), _node("child", deps=("parent",))]
    sub = [_node("p1"), _node("p2")]
    expanded = expand_node(nodes, "parent", sub)
    assert "parent" not in [n.id for n in expanded]
    child = next(n for n in expanded if n.id == "child")
    assert set(child.depends_on) == {"p1", "p2"}


def test_prune_nodes_cleans_edges():
    nodes = [_node("a"), _node("b", deps=("a",)), _node("c", deps=("a",))]
    survivors = prune_nodes(nodes, keep_ids={"a", "b"})
    assert [n.id for n in survivors] == ["a", "b"]
    assert survivors[1].depends_on == ["a"]  # 指向 c 的边已清除


def test_insert_node():
    nodes = [_node("a"), _node("b", deps=("a",))]
    check = _node("check")
    extended = insert_node(nodes, check, after_ids=["a"])
    assert check in extended
    b = next(n for n in extended if n.id == "b")
    assert "check" in b.depends_on  # b 现在依赖检查节点


def test_reroute_node():
    nodes = [_node("a"), _node("b", deps=("a",))]
    rerouted = reroute_node(nodes, "b", new_deps=[])
    assert next(n for n in rerouted if n.id == "b").depends_on == []


def test_merge_nodes():
    nodes = [_node("x", wset=("w1",)), _node("y", wset=("w2",)),
             _node("z", deps=("x",))]
    merged = merge_nodes(nodes, ["x", "y"])
    ids = [n.id for n in merged]
    assert "x" not in ids and "y" not in ids
    merged_node = next(n for n in merged if n.id.startswith("x+"))
    assert set(merged_node.write_set) == {"w1", "w2"}
    z = next(n for n in merged if n.id == "z")
    assert z.depends_on == [merged_node.id]  # 依赖重指向合并节点


# ---------------------------------------------------------------------------
# NFR 一等节点
# ---------------------------------------------------------------------------

def test_nfr_requirements_scale_with_risk():
    low = task_profile("把订单页面顶部三个按钮对齐，并统一间距")
    low_nfr = nfr_requirements({"mode": low["mode"], "profile": low["profile"]})
    assert not any(n["id"] == "nfr-security" for n in low_nfr)
    high = task_profile("在 snaplink-console 增加文件审批，审批后记录审计并通知申请人")
    high_nfr = nfr_requirements({"mode": high["mode"],
                                 "profile": high["profile"]})
    ids = {n["id"] for n in high_nfr}
    assert "nfr-security" in ids
    assert "nfr-rollback-drill" in ids
    assert "nfr-performance" in ids


def test_nfr_in_profile_output():
    report = task_profile("支付审批流程，生产环境")
    assert report["nfr_nodes"]


# ---------------------------------------------------------------------------
# 模型缓存感知放置
# ---------------------------------------------------------------------------

def _device(name, model_cached=None):
    labels = {}
    if model_cached:
        labels[f"model.{model_cached}.cached"] = True
    return {"name": name, "device_id": f"d-{name}",
            "static_capabilities": {"os": "Linux"},
            "runtimes": {}, "labels": labels,
            "dynamic_state": {"status": "online"},
            "trust": {"max_effect_class": "read_only"},
            "capability_valid_until": "2099-01-01T00:00:00"}


def test_model_cached_label():
    devices = [_device("cached-node", model_cached="qwen3-35b")]
    assert _model_cached("cached-node", "qwen3-35b", devices) is True
    assert _model_cached("cached-node", "other-model", devices) is False


def test_pick_device_prefers_model_cached():
    """模型权重已缓存的设备优先（AADM-D §14 模型缓存感知）。"""
    from pbatch.fabric_devices import TaskPlacement
    devices = [_device("plain"), _device("cached", model_cached="qwen3-35b")]
    chosen = pick_device(TaskPlacement(), devices,
                         model="qwen3-35b")
    assert chosen == "cached"
    # 不指定模型：负载相同 → 默认取第一个
    chosen2 = pick_device(TaskPlacement(), devices)
    assert chosen2 in ("plain", "cached")
