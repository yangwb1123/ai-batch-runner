"""Tests for 分阶段竞跑 / 波次调度 / 契约 / 真值维护（AADM-R §10-12, AADM-G §3/20）.

racing: successive-halving 淘汰、保守淘汰（低置信度多保留）、逐轮历史。
waves: 拓扑波次、写冲突分解、串行化防活锁、上下文聚类分配。
contracts: 上游保证 ⊆ 下游假设、全图检查、语义校验。
truth: 前提失效级联、日志回放、幂等。
"""

from __future__ import annotations

import json

import pytest

from pbatch import truth
from pbatch.contracts import (NodeContract, check_graph, compose_check,
                              validate_semantics)
from pbatch.fabric_sched import TaskNode
from pbatch.racing import (RaceCandidate, load_race_input, staged_race,
                           successive_halving)
from pbatch.waves import assign_agents, context_similarity, plan_waves


# ---------------------------------------------------------------------------
# racing：分阶段竞跑（Successive Halving）
# ---------------------------------------------------------------------------

def _candidates(ids):
    return [RaceCandidate(id=cid) for cid in ids]


def test_successive_halving_eliminates_half():
    candidates = _candidates(["a", "b", "c", "d"])
    scores = {"a": 0.9, "b": 0.7, "c": 0.5, "d": 0.3}
    survivors = successive_halving(candidates, scores)
    assert [c.id for c in survivors] == ["a", "b"]  # 3→2（3 个保留 2）
    assert candidates[2].eliminated and candidates[3].eliminated


def test_successive_halving_keep_min():
    candidates = _candidates(["a", "b"])
    survivors = successive_halving(candidates, {"a": 0.9, "b": 0.8},
                                   keep_min=1)
    assert len(survivors) == 1  # 2 个保留 1
    assert survivors[0].id == "a"


def test_successive_halving_keeps_all_when_at_minimum():
    candidates = _candidates(["a"])
    assert successive_halving(candidates, {"a": 0.5}) == candidates


def test_low_confidence_conservative_retention():
    """信号不可靠（置信度低）时不强制减半——多保留一条路径。"""
    candidates = _candidates(["a", "b", "c", "d"])
    survivors = successive_halving(
        candidates, {"a": 0.9, "b": 0.7, "c": 0.5, "d": 0.3},
        confidence={"a": 0.4})  # 最高分候选置信度低
    assert len(survivors) == 3  # 保留 3 而非 2（保守淘汰）


def test_staged_race_history():
    candidates = _candidates(["sync", "outbox", "workflow"])
    stages = [
        {"name": "plan", "scores": {"sync": 0.9, "outbox": 0.8,
                                    "workflow": 0.4}, "keep_min": 2},
        {"name": "spike", "scores": {"sync": 0.5, "outbox": 0.9}},
    ]
    history = staged_race(candidates, stages)
    assert history[0]["alive"] == ["sync", "outbox"]  # plan 轮淘汰 workflow
    assert history[1]["alive"] == ["outbox"]          # spike 轮淘汰 sync
    assert [c.id for c in candidates if not c.eliminated] == ["outbox"]


def test_race_cli(tmp_path, capsys):
    from pbatch.racing import race_main
    payload = {
        "candidates": {"a": "同步", "b": "Outbox", "c": "工作流引擎"},
        "stages": [
            {"name": "plan", "scores": {"a": 0.9, "b": 0.8, "c": 0.4},
             "keep_min": 2},
            {"name": "spike", "scores": {"a": 0.4, "b": 0.9}},
        ],
    }
    path = tmp_path / "race.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        race_main(["--input", str(path), "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["survivors"] == ["b"]
    assert "c" in report["eliminated"]  # plan 轮淘汰工作流引擎（c）
    assert "a" in report["eliminated"]  # spike 轮淘汰同步（a）


# ---------------------------------------------------------------------------
# waves：波次调度 + Agent 分配
# ---------------------------------------------------------------------------

def _node(nid, deps=(), wset=(), rset=()):
    return TaskNode(id=nid, goal=f"goal-{nid}", argv=["echo", nid],
                    depends_on=list(deps), write_set=list(wset),
                    read_set=list(rset))


def test_plan_waves_topological():
    nodes = [_node("a"), _node("b", deps=("a",)), _node("c", deps=("a",)),
             _node("d", deps=("b", "c"))]
    waves = plan_waves(nodes)
    assert waves[0] == ["a"]
    assert sorted(waves[1]) == ["b", "c"]
    assert waves[2] == ["d"]


@pytest.mark.property
def test_plan_waves_write_conflict_deferred():
    """写同一对象的两节点不能同波（W_i ∩ W_j ≠ ∅ → 推迟）。"""
    nodes = [_node("a", deps=(), wset=("file-x",)),
             _node("b", deps=(), wset=("file-x",)),
             _node("c", deps=())]
    waves = plan_waves(nodes)
    assert len(waves[0]) == 2  # a 和 c（无冲突）同波
    assert waves[1] == ["b"]  # b 因写冲突推迟


def test_plan_waves_serializes_on_full_conflict():
    """整波全冲突 → 串行化第一个，其余下波（防活锁）。"""
    nodes = [_node("a", wset=("x",)), _node("b", wset=("x",)),
             _node("c", wset=("x",))]
    waves = plan_waves(nodes)
    assert len(waves) == 3  # 一波一个
    assert [w[0] for w in waves] == ["a", "b", "c"]


def test_plan_waves_max_wave():
    nodes = [_node("a"), _node("b"), _node("c"), _node("d")]
    waves = plan_waves(nodes, max_wave=2)
    assert len(waves[0]) == 2 and len(waves[1]) == 2


def test_context_similarity():
    a = _node("a")
    b = _node("b", deps=("x",))
    c = _node("c", deps=("x",))
    d = _node("d")
    assert context_similarity(b, c) > context_similarity(a, d)
    assert context_similarity(a, d) == 0.1


def test_assign_agents_clusters_related():
    """共享依赖的节点应聚到同一 Agent（上下文连续性 > 平均分工）。"""
    nodes = [_node("a", deps=("x",)), _node("b", deps=("x",)),
             _node("c", deps=("y",))]
    clusters = assign_agents(nodes, agent_count=2)
    flat = [n.id for cluster in clusters for n in cluster]
    assert sorted(flat) == ["a", "b", "c"]
    # a 与 b（共享依赖 x）应在同一簇
    for cluster in clusters:
        ids = {n.id for n in cluster}
        if "a" in ids:
            assert "b" in ids or len(cluster) == 1


def test_assign_agents_single_cluster():
    nodes = [_node("a"), _node("b")]
    assert len(assign_agents(nodes, agent_count=1)) == 1


# ---------------------------------------------------------------------------
# contracts：假设-保证契约
# ---------------------------------------------------------------------------

@pytest.mark.contract
def test_compose_check_satisfied():
    upstream = NodeContract(node_id="auth", guarantees=["user_verified",
                                                        "permission_set"])
    downstream = NodeContract(node_id="approve",
                              assumptions=["user_verified",
                                           "permission_set"])
    assert compose_check(upstream, downstream) == []


def test_compose_check_violation():
    upstream = NodeContract(node_id="auth", guarantees=["user_verified"])
    downstream = NodeContract(node_id="approve",
                              assumptions=["user_verified",
                                           "permission_contains_approve"])
    violations = compose_check(upstream, downstream)
    assert len(violations) == 1
    assert violations[0]["assumption"] == "permission_contains_approve"


@pytest.mark.contract
def test_check_graph_detects_chain_violation():
    """A 保证 → B 假设不满足；B 保证 → C 满足；依赖缺失也报。"""
    nodes = [
        TaskNode(id="auth", goal="auth", argv=[], guarantees=["user_verified"]),
        TaskNode(id="approve", goal="approve", argv=[], depends_on=("auth",),
                 assumptions=["user_verified", "file_pending"]),
        TaskNode(id="notify", goal="notify", argv=[], depends_on=("approve",),
                 assumptions=["approved"]),
    ]
    violations = check_graph(nodes)
    reasons = [v["reason"] for v in violations]
    assert any("missing" in r for r in reasons)          # file_pending 未保证
    assert not any(v["downstream"] == "notify"
                   and v["reason"] == "unknown dependency"
                   for v in violations)


def test_check_graph_unknown_dependency():
    nodes = [TaskNode(id="a", goal="a", argv=[], depends_on=("ghost",))]
    violations = check_graph(nodes)
    assert any(v["reason"] == "unknown dependency" for v in violations)


def test_validate_semantics():
    good = NodeContract(node_id="x", failure_semantics="compensatable")
    assert validate_semantics(good) == []
    bad = NodeContract(node_id="x", failure_semantics="explode")
    assert any("invalid failure_semantics" in p for p in validate_semantics(bad))
    contradictory = NodeContract(node_id="x", invariants=["state-ok"],
                                 forbidden_states=["state-ok"])
    assert any("自相矛盾" in p for p in validate_semantics(contradictory))


# ---------------------------------------------------------------------------
# truth：真值维护
# ---------------------------------------------------------------------------

@pytest.mark.property
def test_truth_invalidation_cascade():
    store = truth.TruthStore()
    store.add("DOC-V1", "接口文档 v1", confidence=0.9)
    store.add("API-PAGINATION", "API 支持分页", premises=["DOC-V1"])
    store.add("FRONTEND-ONLY", "前端只做接入", premises=["API-PAGINATION"])
    store.add("UNRELATED", "无关结论")
    affected = store.invalidate("DOC-V1", "文档更新")
    assert affected == ["API-PAGINATION", "DOC-V1", "FRONTEND-ONLY"]
    assert store.status("API-PAGINATION")["status"] == "invalidated"
    assert store.status("UNRELATED")["status"] == "active"


def test_truth_invalidate_idempotent():
    store = truth.TruthStore()
    store.add("P", "前提")
    store.add("C", "结论", premises=["P"])
    store.invalidate("P")
    store.invalidate("P")  # 二次失效：不重复级联
    assert store.status("P")["status"] == "invalidated"
    assert store.status("C")["status"] == "invalidated"


def test_truth_log_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(truth, "TRUTH_FILE", tmp_path / "truth.jsonl")
    truth._record_to_log(truth.TruthStore(), "claim",
                         {"id": "P", "statement": "前提", "premises": [],
                          "confidence": 0.9})
    truth._record_to_log(truth.TruthStore(), "claim",
                         {"id": "C", "statement": "结论", "premises": ["P"],
                          "confidence": 0.7})
    truth._record_to_log(truth.TruthStore(), "invalidate",
                         {"id": "P", "reason": "过期"})
    store = truth.load_from_log(tmp_path / "truth.jsonl")
    assert store.status("C")["status"] == "invalidated"  # 回放含级联
    assert store.status("P")["invalidated_by"] == "过期"


def test_truth_log_skips_bad_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(truth, "TRUTH_FILE", tmp_path / "truth.jsonl")
    truth._record_to_log(truth.TruthStore(), "claim",
                         {"id": "OK", "statement": "好", "premises": [],
                          "confidence": 1.0})
    with (tmp_path / "truth.jsonl").open("a", encoding="utf-8") as f:
        f.write("not-json\n")
    store = truth.load_from_log(tmp_path / "truth.jsonl")
    assert store.status("OK")["status"] == "active"
