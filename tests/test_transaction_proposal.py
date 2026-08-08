"""Tests for 决策事务状态机 / 多 Agent 提案 / Pinned 上下文 / 注册表集成.

transaction: 状态迁移合法性、终态、验证义务、最小事务六条标准。
proposal: 提案冲突检测（契约分歧/风险不一致）、合并去重、收益公式。
pinned: 不可压缩上下文渲染、源指纹、摘要失效检测。
fabric_devices: runners.jsonl 注册设备并入 devices list。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import pinned, proposal, transaction
from pbatch.fabric_devices import DEVICE_STATE_DIR, device_inventory
from pbatch.transaction import (DecisionTransaction, can_transition,
                                is_terminal, transition,
                                validate_minimality, verify_transaction)


# ---------------------------------------------------------------------------
# transaction：决策事务状态机
# ---------------------------------------------------------------------------

def test_transition_lifecycle():
    tx = DecisionTransaction(id="t1", goal="审批")
    transition(tx, "proposed")
    transition(tx, "authorized")
    transition(tx, "executing")
    transition(tx, "observed")
    transition(tx, "verified")
    assert is_terminal(tx.status)


def test_transition_failure_path():
    tx = DecisionTransaction(id="t2", goal="迁移")
    transition(tx, "proposed")
    transition(tx, "authorized")
    transition(tx, "executing")
    transition(tx, "failed")
    transition(tx, "rolled_back")
    assert is_terminal(tx.status)


def test_illegal_transition_rejected():
    tx = DecisionTransaction(id="t3", goal="x")
    with pytest.raises(ValueError):
        transition(tx, "verified")  # draft 不能直接 verified
    with pytest.raises(ValueError):
        transition(tx, "rolled_back")  # 未失败不能回滚
    assert can_transition("draft", "proposed") is True
    assert can_transition("verified", "executing") is False


def test_verify_transaction_evidence():
    tx = DecisionTransaction(id="t4", goal="写代码",
                             expected_evidence=["test_pass",
                                                "typecheck_pass"])
    assert verify_transaction(tx, {"test_pass": True, "typecheck_pass": True})
    assert not verify_transaction(tx, {"test_pass": True})


def test_minimality_six_criteria():
    good = DecisionTransaction(id="t5", goal="单一目标", write_set=["x"],
                               expected_evidence=["test_pass"],
                               compensation="回滚")
    assert validate_minimality(good) == []
    bad = DecisionTransaction(id="t6", goal="", write_set=["a", "b", "c"],
                              expected_evidence=[], compensation="")
    violations = validate_minimality(bad)
    assert any("目标" in v for v in violations)
    assert any("拆分" in v for v in violations)
    assert any("验证义务" in v for v in violations)
    assert any("补偿" in v for v in violations)


# ---------------------------------------------------------------------------
# proposal：多 Agent 提案
# ---------------------------------------------------------------------------

def _proposal(agent_id, contracts=(), findings=(), risks=()):
    return proposal.AgentProposal(agent_id=agent_id,
                                  affected_contracts=list(contracts),
                                  findings=list(findings), risks=list(risks))


def test_detect_contract_conflicts():
    proposals = [_proposal("A", contracts=("api-v1",)),
                 _proposal("B", contracts=("api-v1",))]
    conflicts = proposal.detect_conflicts(proposals)
    assert any(c["type"] == "contract_disagreement"
               and c["contract"] == "api-v1" for c in conflicts)


def test_detect_finding_risk_mismatch():
    """相同发现但风险认知不一致 → 冲突（不能顺着 A 的理由复述）。"""
    proposals = [_proposal("A", findings=("接口慢",), risks=()),
                 _proposal("B", findings=("接口慢",), risks=("高风险",))]
    conflicts = proposal.detect_conflicts(proposals)
    assert any(c["type"] == "finding_without_shared_risk" for c in conflicts)


def test_merge_proposals_dedup():
    proposals = [_proposal("A", findings=("f1", "f2"), risks=("r1",)),
                 _proposal("B", findings=("f2", "f3"), risks=("r2",))]
    merged = proposal.merge_proposals(proposals)
    assert set(merged["findings"]) == {"f1", "f2", "f3"}
    assert set(merged["risks"]) == {"r1", "r2"}


def test_agent_benefit_formula():
    """收益为正才拆分；协调/合并成本高 → 单 Agent 多视角更好。"""
    split = proposal.agent_benefit(parallel_benefit=2.0, specialization=1.0,
                                   independent_review=1.0,
                                   coordination_cost=0.5,
                                   context_sync_cost=0.5, merge_risk=0.5)
    assert split["should_split"] is True
    keep = proposal.agent_benefit(parallel_benefit=0.5, specialization=0.0,
                                  independent_review=0.0,
                                  coordination_cost=1.0,
                                  context_sync_cost=1.0, merge_risk=1.0)
    assert keep["should_split"] is False


def test_proposal_cli(tmp_path, capsys):
    from pbatch.proposal import proposal_main
    payload = {"proposals": [
        {"agent_id": "A", "affected_contracts": ["api-v1"],
         "findings": ["f1"], "risks": [], "assumptions": [],
         "proposed_plan": "", "required_evidence": [], "conflicts": []},
        {"agent_id": "B", "affected_contracts": ["api-v1"],
         "findings": ["f1"], "risks": ["高风险"], "assumptions": [],
         "proposed_plan": "", "required_evidence": [], "conflicts": []},
    ]}
    path = tmp_path / "p.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        proposal_main(["--input", str(path), "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["conflicts"]
    assert report["benefit"]["benefit"] == 0.0  # 默认全 0


# ---------------------------------------------------------------------------
# pinned：不可压缩上下文 + 摘要失效
# ---------------------------------------------------------------------------

def test_pinned_render_and_fingerprint(tmp_path, monkeypatch):
    pinned_file = tmp_path / ".pi-batch" / "pinned.md"
    pinned_file.parent.mkdir(parents=True)
    pinned_file.write_text("不得修改公共 API\n", encoding="utf-8")
    monkeypatch.setattr(pinned, "PINNED_DEFAULT",
                        Path(".pi-batch") / "pinned.md")
    report = pinned.render_pinned(cwd=str(tmp_path))
    assert report["sources"]
    assert "不得修改公共 API" in report["content"]
    assert report["fingerprint"]
    # 摘要有效性：文件未变 → 有效
    assert pinned.summary_valid(report["fingerprint"], cwd=str(tmp_path))
    # 文件变化 → 失效（必须重新生成）
    pinned_file.write_text("不得修改公共 API——已更新\n", encoding="utf-8")
    assert not pinned.summary_valid(report["fingerprint"], cwd=str(tmp_path))


def test_pinned_missing_default_is_empty(tmp_path):
    report = pinned.render_pinned(cwd=str(tmp_path))
    assert report["sources"] == []
    assert report["content"] == ""


def test_pinned_extra_files(tmp_path):
    extra = tmp_path / "contract.md"
    extra.write_text("契约 v2\n", encoding="utf-8")
    report = pinned.render_pinned(cwd=str(tmp_path), extra_files=["contract.md"])
    assert "契约 v2" in report["content"]


def test_pinned_cli(tmp_path, capsys, monkeypatch):
    from pbatch.pinned import pinned_main
    pinned_file = tmp_path / "pinned.md"
    pinned_file.write_text("安全规则", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        pinned_main(["--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["fingerprint"]
    assert "cache_valid" not in report  # 未给缓存指纹时不判断


# ---------------------------------------------------------------------------
# fabric_devices：Runner 注册表并入 devices list
# ---------------------------------------------------------------------------

def test_device_inventory_merges_registered_runners(tmp_path, monkeypatch):
    """runners.jsonl 中的 Runner 设备进入清单（status=registered）。"""
    monkeypatch.setattr("pbatch.fabric_devices.DEVICE_STATE_DIR", tmp_path)
    runners = tmp_path / "runners.jsonl"
    runners.write_text(json.dumps({
        "device_id": "rn-abcdef", "name": "runner-1",
        "secret": "s", "registered_at": "now"}) + "\n", encoding="utf-8")
    nodes = device_inventory(static=[], use_cache=False)
    runner = [n for n in nodes if n["device_id"] == "rn-abcdef"]
    assert runner
    assert runner[0]["dynamic_state"]["status"] == "registered"
    assert runner[0]["transports"] == [{"type": "runner"}]


def test_devices_list_shows_runners(tmp_path, monkeypatch, capsys):
    """CLI `devices list` 集成视图：注册 Runner 显示为 registered。"""
    from pbatch import fabric_cli
    monkeypatch.setattr("pbatch.fabric_devices.DEVICE_STATE_DIR", tmp_path)
    (tmp_path / "runners.jsonl").write_text(json.dumps({
        "device_id": "rn-1234", "name": "pi-node",
        "secret": "s", "registered_at": "now"}) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        fabric_cli.devices_main(["list", "--json"])
    assert exc.value.code == 0
    nodes = json.loads(capsys.readouterr().out)
    assert any(n["device_id"] == "rn-1234"
               and n["dynamic_state"]["status"] == "registered"
               for n in nodes)
