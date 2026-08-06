#!/usr/bin/env python3
"""Change Manifest 门禁：跨仓库变更清单校验（Multi-Project Spec Section 九）。

校验：change_id/title/primary_project/affected_projects/failure_policy/tests
必填；受影响项目必须存在且与 unaffected 无交集；失败策略必须包含
audit 与 notification 的明确语义（durable_async / best_effort_async /
fail_closed / fail_open / retry）。

用法：
  python check-change-manifest.py MANIFEST.yaml [--map platform-map.yaml] [--json]
支持 JSON 或受限 YAML 输入（按首字符自动识别）。
退出码：0 = 通过；1 = 校验失败；2 = 用法/配置错误。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_common import load_platform_map, parse_mini_yaml, project_names  # noqa: E402

REQUIRED = ["change_id", "title", "primary_project", "affected_projects",
            "failure_policy", "tests"]
VALID_FAILURE = {"durable_async", "best_effort_async", "fail_closed",
                 "fail_open", "retry_async", "local_queue"}
REQUIRED_FAILURE = {"audit", "notification"}


def _check_required_fields(m: dict, errors: list) -> None:
    """必填字段存在性 + change_id 非空。"""
    for field in REQUIRED:
        if field not in m:
            errors.append(f"{field}: missing")
    if not isinstance(m.get("change_id"), str) or not m["change_id"].strip():
        errors.append("change_id: must be a non-empty string")


def _check_projects(m: dict, known: set, errors: list) -> None:
    """主责任/受影响/不受影响项目引用与交集。"""
    primary = m.get("primary_project")
    if primary not in known:
        errors.append(f"primary_project: unknown project '{primary}'")
    affected = m.get("affected_projects")
    if not isinstance(affected, list) or not affected:
        errors.append("affected_projects: must be a non-empty list")
    else:
        for name in affected:
            if name not in known:
                errors.append(f"affected_projects: unknown project '{name}'")
    for name in m.get("unaffected_projects") or []:
        if name not in known:
            errors.append(f"unaffected_projects: unknown project '{name}'")
    overlap = set(affected or []) & set(m.get("unaffected_projects") or [])
    if overlap:
        errors.append(f"project in both affected and unaffected: {sorted(overlap)}")


def _check_failure_policy(m: dict, errors: list) -> None:
    """失败策略必须含 audit 与 notification 的明确语义。"""
    fp = m.get("failure_policy")
    if not isinstance(fp, dict):
        errors.append("failure_policy: must be a mapping")
        return
    for key in REQUIRED_FAILURE:
        if key not in fp:
            errors.append(f"failure_policy.{key}: missing")
        elif fp[key] not in VALID_FAILURE:
            errors.append(f"failure_policy.{key}: invalid value '{fp[key]}'")


def check_manifest(m: dict, pmap: dict) -> list:
    """返回错误列表；空列表 = 通过。"""
    errors: list = []
    _check_required_fields(m, errors)
    _check_projects(m, project_names(pmap), errors)
    _check_failure_policy(m, errors)
    compat = m.get("compatibility") or {}
    if "backward_compatible" not in compat:
        errors.append("compatibility.backward_compatible: missing")
    return errors


def load_manifest(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    if raw.lstrip().startswith("{"):
        data = json.loads(raw)
    else:
        data = parse_mini_yaml(raw)
    if not isinstance(data, dict):
        raise ValueError("manifest must be a mapping")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="change manifest file (JSON or YAML)")
    parser.add_argument("--map", default="platform-map.yaml",
                        help="platform map path (default: ./platform-map.yaml)")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args()
    try:
        pmap = load_platform_map(Path(args.map).resolve().parent)
        m = load_manifest(Path(args.manifest))
        errors = check_manifest(m, pmap)
    except (ValueError, json.JSONDecodeError) as exc:
        errors = [str(exc)]
    report = {"ok": not errors, "errors": errors}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for e in errors:
            print(f"MANIFEST-GATE: {e}")
        print("MANIFEST-GATE: PASS" if report["ok"] else "MANIFEST-GATE: FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
