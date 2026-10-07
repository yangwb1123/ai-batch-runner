"""Explicit local request registration/withdrawal; no executable repair command."""
from __future__ import annotations

import json

from .task_center_io import display
from .task_goal_store import _actor, resolve_source
from .task_repair_binding import read_manifest
from . import task_repair_store as store


def add_parser(sub) -> None:
    parser = sub.add_parser("repairs", help="有界恢复请求登记/撤回（不是执行授权）")
    actions = parser.add_subparsers(dest="repair_action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--source", default="")
    listing.add_argument("--json", action="store_true")
    showing = actions.add_parser("show")
    showing.add_argument("repair_id")
    checking = actions.add_parser("check", help="只读检查登记请求；非实时预检，阻断返回 3")
    checking.add_argument("repair_id")
    checking.add_argument("--source", default="")
    checking.add_argument("--json", action="store_true")
    inspecting = actions.add_parser("inspect-workspace", help="显式一次性 Git 诊断；不存回执、不授执行权")
    inspecting.add_argument("repair_id")
    inspecting.add_argument("--confirm", action="store_true")
    inspecting.add_argument("--expected-head", required=True, help="operator-supplied full OID, not original run evidence")
    inspecting.add_argument("--common-dir", default="", help="explicit metadata scope for a linked worktree")
    inspecting.add_argument("--source", default="")
    inspecting.add_argument("--json", action="store_true")
    registering = actions.add_parser("register")
    registering.add_argument("--source", required=True)
    registering.add_argument("--file", required=True, help="explicit JSON inside registered source roots")
    registering.add_argument("--confirm", action="store_true", help="confirm metadata only, never execution")
    withdrawing = actions.add_parser("withdraw")
    withdrawing.add_argument("repair_id")
    withdrawing.add_argument("--if-version", type=int, required=True)
    for action in (registering, withdrawing):
        action.add_argument("--by", required=True, help="unauthenticated local audit label")
        action.add_argument("--reason", required=True)


def command(args, _cwd: str) -> int:
    try:
        if args.repair_action in {"list", "show", "check"}:
            return _read(args)
        if args.repair_action == "inspect-workspace":
            return _workspace_report(args)
        _actor(args.by, args.reason)
        if args.repair_action == "register":
            if not args.confirm:
                raise ValueError("repair request registration needs explicit --confirm; no file was read")
            source = resolve_source(args.source)
            manifest = read_manifest(source, args.file)
            value = store.register(source["source_id"], manifest, args.by, args.reason)
        else:
            value = store.withdraw(args.repair_id, args.by, args.reason, args.if_version)
        print(json.dumps({"repair_id": value["repair_id"], "version": value["version"],
                          "state": value["state"], "grants_execution": False}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print("ops repairs: " + display(str(exc), 1024))
        return 2


def _read(args) -> int:
    from .task_center import snapshot
    view = snapshot(source_id=getattr(args, "source", ""))
    if args.repair_action in {"show", "check"}:
        record = next((row for row in view["repairs"] if row["repair_id"] == args.repair_id), None)
        if record is None:
            print("repair request not found in registered bounded view")
            return 1
        if args.repair_action == "check":
            return _check_report(record, args.json)
        print(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False))
    elif args.json:
        print(json.dumps({"repairs": view["repairs"], "diagnostics": view["diagnostics"]},
                         ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for row in view["repairs"]:
            print(f"{row['repair_id']} v{row['version']} {row['project']} {row['status']}")
            print("  " + row["receipt"]["stage_label"] + "；声明限制未核验原预算、未授执行权")
            print("  只读检查=" + row["assessment"]["decision"] + "；阻塞=" + ", ".join(row["assessment"]["blockers"]))
        for note in view["diagnostics"]:
            print("NOTE: " + note)
    return 0


def _workspace_report(args) -> int:
    from .task_repair_workspace import inspect_workspace
    report = inspect_workspace(args.repair_id, args.expected_head, confirm=args.confirm,
                               common_dir=args.common_dir, source_id=args.source)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(report["repair_id"] + " 工作区诊断=" + report["inspection_state"] + "（不可执行；不证明原运行归属）")
        if report["git_observation"]:
            observation = report["git_observation"]
            print("  HEAD=" + observation["head"] + "；Git 状态计数=" + json.dumps(observation["status"]["counts"]))
        if report["diagnostic"]:
            print("  诊断=" + report["diagnostic"])
        print("  阻塞=" + ", ".join(report["blockers"]))
    return 3  # even matching/clean Git samples never authorize recovery


def _check_report(record: dict, json_output: bool) -> int:
    report = record["assessment"]
    if json_output:
        print(json.dumps({"repair_id": record["repair_id"], "assessment": report},
                         ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(record["repair_id"] + " 只读检查=" + report["decision"] + "（不可执行；非实时预检）")
        for row in report["metadata_checks"]:
            print("  " + row["id"] + ": " + row["state"])
        print("  尚待独立检查=" + ", ".join(row["id"] for row in report["required_execution_checks"]))
        print("  阻塞=" + ", ".join(report["blockers"]))
    return 3  # blocked/inactive never means an executor was admitted
