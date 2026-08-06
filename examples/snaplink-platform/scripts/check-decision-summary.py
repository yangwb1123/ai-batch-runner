#!/usr/bin/env python3
"""决策摘要门禁（Decision Gate）：组合需求进入编码前的强制检查。

校验 Agent 产出的决策摘要 JSON（Multi-Project Product Design Agent
Specification Section 9 输出格式）。缺任何必填维度、主责任项目不在
platform-map、项目引用非法、组合类型无协作项目 → 校验失败（fail closed）。
校验失败 = 摘要不落盘 = gate 无 VERDICT = 流水线阻断。

用法：
  python check-decision-summary.py SUMMARY.json [--map platform-map.yaml] [--json]
退出码：0 = 通过；1 = 校验失败；2 = 用法/配置错误。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_common import load_platform_map, project_names  # noqa: E402

REQUIRED_SECTIONS = {
    "requirement": ["original_prompt", "interpreted_goal", "user_roles", "success_outcome"],
    "scope": ["type", "priority"],
    "scenario": ["entry", "preconditions", "happy_path", "failure_paths", "follow_up"],
    "capabilities": ["existing", "extend", "create", "excluded"],
    "ownership": ["primary_project", "collaborating_projects",
                  "unaffected_projects", "data_sources_of_truth"],
    "integration": ["synchronous_calls", "async_events", "workflows",
                    "optional_dependencies", "failure_policies"],
    "changes": ["apis", "events", "permissions", "schemas", "migrations",
                "configurations", "ui"],
    "quality": ["security_requirements", "compatibility_requirements",
                "observability_requirements", "test_plan",
                "deployment_order", "rollback_plan"],
}
TOP_LEVEL_FIELDS = ["open_risks"]
SCOPE_TYPES = ("project", "integration", "composition")
LIST_FIELDS = {"collaborating_projects", "unaffected_projects", "existing",
               "extend", "create", "excluded", "happy_path", "failure_paths",
               "follow_up", "preconditions", "open_risks", "test_plan",
               "deployment_order", "rollback_plan", "synchronous_calls",
               "async_events", "workflows", "optional_dependencies",
               "security_requirements", "compatibility_requirements",
               "observability_requirements"}


def check_summary(summary: dict, pmap: dict) -> list:
    """返回错误列表；空列表 = 通过。"""
    errors: list = []
    for field in TOP_LEVEL_FIELDS:
        if not isinstance(summary.get(field), list):
            errors.append(f"{field}: must be a list")
    for section, fields in REQUIRED_SECTIONS.items():
        if not isinstance(summary.get(section), dict):
            errors.append(f"missing section: {section}")
            continue
        for field in fields:
            if field not in summary[section]:
                errors.append(f"{section}.{field}: missing")
            elif field in LIST_FIELDS and not isinstance(summary[section][field], list):
                errors.append(f"{section}.{field}: must be a list")
    scope_type = (summary.get("scope") or {}).get("type")
    if scope_type not in SCOPE_TYPES:
        errors.append(f"scope.type: must be one of {SCOPE_TYPES}")
    _check_projects(summary, pmap, errors)
    return errors


def _check_project_refs(ownership: dict, known: set, errors: list) -> None:
    """主责任/协作/不受影响项目必须都在 platform-map 中。"""
    primary = ownership.get("primary_project")
    if primary not in known:
        errors.append(f"ownership.primary_project: unknown project '{primary}'")
    for key in ("collaborating_projects", "unaffected_projects"):
        for name in ownership.get(key) or []:
            if name not in known:
                errors.append(f"ownership.{key}: unknown project '{name}'")


def _check_composition_rules(ownership: dict, scope_type: str, errors: list) -> None:
    """组合类型必须有协作项目；协作与不受影响不得重叠。"""
    if scope_type == "composition" and not ownership.get("collaborating_projects"):
        errors.append("scope.type=composition requires collaborating_projects")
    overlap = set(ownership.get("collaborating_projects") or []) & \
        set(ownership.get("unaffected_projects") or [])
    if overlap:
        errors.append(f"project listed as both collaborating and unaffected: {sorted(overlap)}")


def _check_failure_policies(summary: dict, errors: list) -> None:
    """集成失败策略必须是非空映射。"""
    fp = (summary.get("integration") or {}).get("failure_policies")
    if not isinstance(fp, dict) or not fp:
        errors.append("integration.failure_policies: must be a non-empty mapping")


def _check_projects(summary: dict, pmap: dict, errors: list):
    """项目引用校验入口：platform-map 非空则逐项检查。"""
    known = project_names(pmap)
    if not known:
        errors.append("platform-map: no projects defined")
        return
    ownership = summary.get("ownership") or {}
    _check_project_refs(ownership, known, errors)
    scope_type = (summary.get("scope") or {}).get("type")
    _check_composition_rules(ownership, scope_type, errors)
    _check_failure_policies(summary, errors)


def load_summary(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read summary: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"summary is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("summary must be a JSON object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", help="decision summary JSON file")
    parser.add_argument("--map", default="platform-map.yaml",
                        help="platform map path (default: ./platform-map.yaml)")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args()
    try:
        pmap = load_platform_map(Path(args.map).resolve().parent)
        summary = load_summary(Path(args.summary))
        errors = check_summary(summary, pmap)
    except ValueError as exc:
        errors = [str(exc)]
    report = {"ok": not errors, "errors": errors}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for e in errors:
            print(f"DECISION-GATE: {e}")
        print("DECISION-GATE: PASS" if report["ok"] else "DECISION-GATE: FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
