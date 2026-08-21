"""Decision transaction state machine (AADM §3).

决策事务是 Agent 的最小闭环交互单元：触发 → 读取状态 → 理解目标 → 检查
约束 → 产生候选行动 → 选择行动 → 改变状态 → 获取证据 → 提交或回滚。

状态机：draft → proposed → authorized → executing → observed → verified
（终态），或任何非终态 → failed → rolled_back（终态）。非法迁移在进入
执行前 fail closed——"修改权限+创建文件+发通知+记审计"不是最小事务，
应拆分后经依赖组合（见 hypergraph）。

纯函数 + 轻量 dataclass，可单测。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config
from .text_io import read_text_bounded

# 合法迁移表（AADM §3 DT.status）。
TRANSITIONS = {
    "draft": {"proposed"},
    "proposed": {"authorized", "failed"},
    "authorized": {"executing", "failed"},
    "executing": {"observed", "failed"},
    "observed": {"verified", "failed"},
    "verified": set(),
    "failed": {"rolled_back"},
    "rolled_back": set(),
}

TERMINAL = {"verified", "rolled_back"}


@dataclass
class ExecutionBudget:
    """执行预算；cost/seconds 为 0 表示不设置上限，risk 始终有上限。"""

    max_cost: float = 0.0
    max_seconds: float = 0.0
    max_risk: float = 1.0
    required_capabilities: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"max_cost": self.max_cost,
                "max_seconds": self.max_seconds,
                "max_risk": self.max_risk,
                "required_capabilities": list(self.required_capabilities)}

    @classmethod
    def from_value(cls, value) -> "ExecutionBudget":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("budget must be a mapping")
        return cls(max_cost=float(value.get("max_cost", 0) or 0),
                   max_seconds=float(value.get("max_seconds", 0) or 0),
                   max_risk=float(value.get("max_risk", 1) or 0),
                   required_capabilities=list(
                       value.get("required_capabilities", []) or []))


@dataclass
class DecisionTransaction:
    """一个决策事务：单一目标、单一状态变化、可独立验证与补偿。"""
    id: str
    goal: str
    task_id: str = ""
    trigger_atom_ids: list = field(default_factory=list)  # 为什么启动
    read_set: list = field(default_factory=list)           # 读取的事实/状态
    goal_atom_id: str = ""                                 # 唯一主要目标
    guard_atom_ids: list = field(default_factory=list)      # 执行前必须成立
    candidate_actions: list = field(default_factory=list)   # 候选行动
    options: list = field(default_factory=list)             # AADM 同义字段
    selected_action: str = ""
    write_set: list = field(default_factory=list)           # 预期改变的状态
    expected_evidence: list = field(default_factory=list)   # 证明义务
    verifier: dict = field(default_factory=dict)            # 如何验证
    compensation: str = ""                                  # 失败回退
    budget: object = field(default_factory=ExecutionBudget)
    status: str = "draft"

    def __post_init__(self) -> None:
        """Normalize JSON/dict inputs while keeping the old constructor API."""
        if self.options and not self.candidate_actions:
            self.candidate_actions = list(self.options)
        elif self.candidate_actions and not self.options:
            self.options = list(self.candidate_actions)
        self.budget = ExecutionBudget.from_value(self.budget)

    def to_dict(self) -> dict:
        """Serialize the complete transaction contract for event/replay use."""
        return {
            "id": self.id, "task_id": self.task_id,
            "trigger_atom_ids": list(self.trigger_atom_ids),
            "read_set": list(self.read_set), "goal": self.goal,
            "goal_atom_id": self.goal_atom_id,
            "guard_atom_ids": list(self.guard_atom_ids),
            "candidate_actions": list(self.candidate_actions),
            "options": list(self.options), "selected_action": self.selected_action,
            "write_set": list(self.write_set),
            "expected_evidence": list(self.expected_evidence),
            "verifier": dict(self.verifier or {}),
            "compensation": self.compensation,
            "budget": self.budget.to_dict(), "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "DecisionTransaction":
        """Load only declared fields; unknown input is rejected, not ignored."""
        if not isinstance(value, dict):
            raise ValueError("transaction must be a mapping")
        allowed = {"id", "task_id", "trigger_atom_ids", "read_set", "goal",
                    "goal_atom_id", "guard_atom_ids", "candidate_actions",
                    "options", "selected_action", "write_set",
                    "expected_evidence", "verifier", "compensation", "budget",
                    "status"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown transaction fields: {', '.join(unknown)}")
        return cls(**value)


def can_transition(current: str, next_state: str) -> bool:
    return next_state in TRANSITIONS.get(current, set())


def transition(tx: DecisionTransaction, next_state: str) -> None:
    """状态迁移；非法迁移抛 ValueError（fail closed）。"""
    if next_state not in TRANSITIONS.get(tx.status, set()):
        raise ValueError(
            f"transaction {tx.id}: 非法迁移 {tx.status} → {next_state}"
            f"（允许: {sorted(TRANSITIONS.get(tx.status, set())) or '终态'}）")
    tx.status = next_state


def is_terminal(state: str) -> bool:
    return state in TERMINAL


def _action_id(action) -> str:
    return str(action.get("id", "")) if isinstance(action, dict) else str(action)


def _contract_basic_violations(tx: DecisionTransaction) -> list:
    """Validate identity, trigger, goal and proof declarations."""
    violations = []
    if not tx.id:
        violations.append("缺少事务 id")
    if not tx.task_id:
        violations.append("缺少 task_id")
    if not tx.trigger_atom_ids:
        violations.append("缺少触发原子 trigger_atom_ids")
    if not tx.goal and not tx.goal_atom_id:
        violations.append("缺少主要目标 goal/goal_atom_id")
    if not tx.expected_evidence:
        violations.append("缺少预期证据 expected_evidence")
    if not isinstance(tx.verifier, dict) or not tx.verifier:
        violations.append("缺少验证器 verifier")
    return violations


def _contract_action_violations(tx: DecisionTransaction) -> list:
    """Validate candidate/selected actions and compensation obligations."""
    violations = []
    actions = [_action_id(action) for action in tx.candidate_actions]
    if not actions:
        violations.append("缺少候选行动 options")
    if tx.selected_action and tx.selected_action not in actions:
        violations.append("selected_action 不在候选行动中")
    if tx.write_set and not tx.compensation:
        violations.append("有状态变化但缺少 compensation")
    return violations


def validate_contract(tx: DecisionTransaction) -> list:
    """Validate the complete AADM contract before authorization/execution."""
    violations = (_contract_basic_violations(tx) +
                  _contract_action_violations(tx))
    if not isinstance(tx.budget, ExecutionBudget):
        violations.append("budget 未规范化")
    if tx.status not in TRANSITIONS:
        violations.append(f"未知事务状态: {tx.status}")
    return violations


def guards_satisfied(tx: DecisionTransaction, satisfied_atoms) -> bool:
    """All guard atoms must be present; an absent guard fails closed."""
    return set(tx.guard_atom_ids) <= set(satisfied_atoms or ())


def budget_check(budget: ExecutionBudget, observed: Optional[dict] = None) -> tuple:
    """Return ``(allowed, violations)`` for observed cost/risk/capabilities."""
    budget = ExecutionBudget.from_value(budget)
    observed = observed or {}
    violations = []
    for key, limit in (("cost", budget.max_cost),
                       ("seconds", budget.max_seconds),
                       ("risk", budget.max_risk)):
        try:
            actual = float(observed.get(key, 0) or 0)
        except (TypeError, ValueError):
            violations.append(f"{key} 不是数字")
            continue
        if (key == "risk" and actual > limit) or (key != "risk" and
                                                   limit > 0 and actual > limit):
            violations.append(f"{key} {actual} 超过预算 {limit}")
    violations.extend(_capability_budget_violations(budget, observed))
    return not violations, violations


def _capability_budget_violations(budget: ExecutionBudget,
                                  observed: dict) -> list:
    """Use explicit grants when supplied; keep legacy token mode compatible."""
    if "capability_grants" in observed:
        from .capabilities import authorize_required_capabilities
        allowed, failures = authorize_required_capabilities(
            budget.required_capabilities, observed.get("capability_grants"),
            observed)
        return [] if allowed else ["能力授权: " + " | ".join(failures)]
    granted = set(observed.get("capabilities", []) or [])
    missing = sorted(set(budget.required_capabilities) - granted)
    return ["缺少能力: " + ", ".join(missing)] if missing else []


def authorize_execution(tx: DecisionTransaction, satisfied_atoms,
                        observed_budget: Optional[dict] = None) -> None:
    """Guard and budget gate between ``authorized`` and ``executing``."""
    if tx.status != "authorized":
        raise ValueError(f"transaction {tx.id}: must be authorized before execution")
    violations = validate_contract(tx)
    if violations:
        raise ValueError("transaction contract invalid: " + "; ".join(violations))
    missing = sorted(set(tx.guard_atom_ids) - set(satisfied_atoms or ()))
    if missing:
        raise ValueError("guards unsatisfied: " + ", ".join(missing))
    allowed, budget_violations = budget_check(tx.budget, observed_budget)
    if not allowed:
        raise ValueError("execution budget rejected: " + "; ".join(budget_violations))
    transition(tx, "executing")


def verify_transaction(tx: DecisionTransaction, evidence: dict) -> bool:
    """验证义务：所有 expected_evidence 必须有 truthy 支持。"""
    if isinstance(evidence, set):
        return all(item in evidence for item in tx.expected_evidence)
    return all(bool(evidence.get(item)) for item in tx.expected_evidence)


def verify_and_transition(tx: DecisionTransaction, evidence: dict) -> bool:
    """Verify an observed transaction and move it to ``verified``/``failed``."""
    if tx.status != "observed":
        raise ValueError(f"transaction {tx.id}: must be observed before verification")
    if verify_transaction(tx, evidence):
        transition(tx, "verified")
        return True
    transition(tx, "failed")
    return False


def rollback_transaction(tx: DecisionTransaction) -> None:
    """Move a failed transaction to rolled_back only with compensation defined."""
    if not tx.compensation:
        raise ValueError(f"transaction {tx.id}: compensation is required")
    transition(tx, "rolled_back")


def validate_minimality(tx: DecisionTransaction) -> list:
    """最小事务六条标准（AADM §3）：单目标/单状态变化/单责任/可验证/
    可重试补偿。返回违规列表。"""
    violations = []
    if not tx.goal:
        violations.append("缺少单一主要目标")
    if len(tx.write_set) > 1:
        violations.append(f"write_set 含 {len(tx.write_set)} 项状态变化"
                          "——应拆分为多个事务经依赖组合")
    if not tx.expected_evidence:
        violations.append("缺少验证义务（expected_evidence）")
    if not tx.compensation and tx.write_set:
        violations.append("有状态变化但无补偿方案")
    return violations


def _read_json(path: str) -> dict:
    try:
        value = json.loads(read_text_bounded(
            Path(path), config.INPUT_MAX_BYTES, "transaction input"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid transaction input: {exc}") from exc
    return value


def transaction_main(argv: Optional[list] = None) -> int:
    """`pi-batch transaction --input FILE [--action validate|authorize|verify|rollback]`."""
    parser = argparse.ArgumentParser(
        prog="pi-batch.py transaction",
        description="Decision Transaction contract and guard/budget gates")
    parser.add_argument("--input", required=True, help="transaction JSON file")
    parser.add_argument("--action", choices=("validate", "authorize", "verify",
                                              "rollback"),
                        default="validate")
    parser.add_argument("--satisfied", default="",
                        help="comma-separated satisfied guard atom ids")
    parser.add_argument("--evidence", default="{}",
                        help="JSON object of observed proof evidence")
    parser.add_argument("--budget", default="{}",
                        help="JSON object of observed cost/seconds/risk/capabilities")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        tx = DecisionTransaction.from_dict(_read_json(args.input))
        report = {"transaction": tx.to_dict()}
        if args.action == "validate":
            violations = validate_contract(tx)
            report.update(valid=not violations, violations=violations)
        elif args.action == "authorize":
            authorize_execution(tx, [item for item in args.satisfied.split(",") if item],
                                json.loads(args.budget))
            report.update(valid=True, violations=[], transaction=tx.to_dict())
        elif args.action == "verify":
            evidence = json.loads(args.evidence)
            verified = verify_and_transition(tx, evidence)
            report.update(valid=verified, violations=[] if verified else
                          ["预期证据不足"], transaction=tx.to_dict())
        else:
            rollback_transaction(tx)
            report.update(valid=True, violations=[], transaction=tx.to_dict())
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        report = {"valid": False, "violations": [str(exc)]}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("TRANSACTION: " + ("VALID" if report.get("valid") else "INVALID"))
        for violation in report.get("violations", []):
            print(f"  - {violation}")
    raise SystemExit(0 if report.get("valid") else 1)
