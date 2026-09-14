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
from pbatch.transaction import (DecisionTransaction, ExecutionBudget,
                                authorize_execution, can_transition,
                                is_terminal, transition, validate_contract,
                                validate_minimality, rollback_transaction,
                                verify_and_transition, verify_transaction)


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


def _complete_transaction(status="draft"):
    return DecisionTransaction(
        id="tx-full", task_id="task-1", goal="审批文件",
        goal_atom_id="goal-1", trigger_atom_ids=["request-1"],
        read_set=["file-1", "approval-1"], guard_atom_ids=["can-approve"],
        candidate_actions=[{"id": "approve", "description": "通过"}],
        selected_action="approve", write_set=["approval-1.status"],
        expected_evidence=["test_pass"], verifier={"kind": "command"},
        compensation="restore approval-1.status",
        budget=ExecutionBudget(max_cost=2, max_seconds=10, max_risk=0.5,
                               required_capabilities=["approval.write"]),
        status=status,
    )


def test_full_transaction_contract_roundtrip():
    tx = _complete_transaction()
    assert validate_contract(tx) == []
    restored = DecisionTransaction.from_dict(tx.to_dict())
    assert restored.to_dict() == tx.to_dict()


def test_authorize_execution_is_guard_and_budget_fail_closed():
    tx = _complete_transaction("authorized")
    with pytest.raises(ValueError, match="guards unsatisfied"):
        authorize_execution(tx, [], {"capabilities": ["approval.write"]})
    assert tx.status == "authorized"
    with pytest.raises(ValueError, match="超过预算"):
        authorize_execution(tx, ["can-approve"],
                            {"cost": 3, "capabilities": ["approval.write"]})
    assert tx.status == "authorized"
    authorize_execution(tx, ["can-approve"],
                        {"cost": 1, "seconds": 2, "risk": 0.2,
                         "capabilities": ["approval.write"]})
    assert tx.status == "executing"


def test_authorize_execution_can_enforce_capability_grants():
    tx = _complete_transaction("authorized")
    grant = {
        "agent_id": "agent-a", "node_id": "node-1",
        "actions": ["approval.write"], "resources": ["approval:*"],
        "environment": "trusted-local", "expires_at": "2099-01-01T00:00:00Z",
        "max_side_effect_class": "reversible",
    }
    observed = {
        "agent_level": 3, "agent_id": "agent-a", "node_id": "node-1",
        "action": "approval.write", "resource": "approval:1",
        "environment": "trusted-local", "device_zone": "trusted-local",
        "effect_class": "reversible", "capability_grants": [grant],
    }
    authorize_execution(tx, ["can-approve"], observed)
    assert tx.status == "executing"

    denied = _complete_transaction("authorized")
    observed["node_id"] = "other-node"
    with pytest.raises(ValueError, match="能力授权"):
        authorize_execution(denied, ["can-approve"], observed)
    assert denied.status == "authorized"


def test_verify_and_transition_rejects_false_evidence():
    tx = _complete_transaction("observed")
    assert verify_transaction(tx, {"test_pass": False}) is False
    assert verify_and_transition(tx, {"test_pass": False}) is False
    assert tx.status == "failed"


def test_rollback_requires_failed_state_and_compensation():
    tx = _complete_transaction("failed")
    rollback_transaction(tx)
    assert tx.status == "rolled_back"
    active = _complete_transaction("authorized")
    with pytest.raises(ValueError):
        rollback_transaction(active)


def test_transaction_cli_validate(tmp_path, capsys):
    from pbatch.transaction import transaction_main
    source = tmp_path / "transaction.json"
    source.write_text(json.dumps(_complete_transaction().to_dict()),
                      encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        transaction_main(["--input", str(source), "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is True
    assert report["transaction"]["budget"]["max_cost"] == 2


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
    from pbatch import config
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "private-runtime"))
    pinned_file = config.data_dir(str(tmp_path)) / "pinned.md"
    pinned_file.parent.mkdir(parents=True)
    pinned_file.write_text("不得修改公共 API\n", encoding="utf-8")
    report = pinned.render_pinned(cwd=str(tmp_path))
    assert report["sources"] == [str(pinned_file)]
    assert not (tmp_path / ".pi-batch").exists()
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
