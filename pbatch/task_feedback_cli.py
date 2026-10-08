"""Explicit local minimal feedback drafting and permanent withdrawal; no execution/upload."""
from __future__ import annotations

import json

from . import task_feedback_store as store
from .task_center_io import display
from .task_feedback_schema import FEEDBACK_PATTERN


def add_parser(sub) -> None:
    parser = sub.add_parser("feedback", help="本地合成反馈草案（不复现、不上传、不发布）")
    actions = parser.add_subparsers(dest="feedback_action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--source", default="")
    listing.add_argument("--json", action="store_true")
    showing = actions.add_parser("show")
    showing.add_argument("feedback_id")
    showing.add_argument("--source", default="")
    verifying = actions.add_parser("verify-copy", help="显式复核私有副本字节；不打开原文件、不复现案例")
    verifying.add_argument("feedback_id")
    verifying.add_argument("--confirm", action="store_true")
    drafting = actions.add_parser("draft", help="显式复制登记根内的纯 JSON 合成案例；不执行正文")
    drafting.add_argument("--source", required=True)
    drafting.add_argument("--file", required=True)
    drafting.add_argument("--confirm", action="store_true")
    withdrawing = actions.add_parser("withdraw", help="永久撤回；保留私有副本与完整审计")
    withdrawing.add_argument("feedback_id")
    withdrawing.add_argument("--if-version", type=int, required=True)
    for action in (drafting, withdrawing):
        action.add_argument("--by", required=True, help="unauthenticated local audit label")
        action.add_argument("--reason", required=True)


def command(args, _cwd: str) -> int:
    try:
        if args.feedback_action in {"list", "show"}:
            return _read(args)
        if args.feedback_action == "verify-copy":
            from .task_feedback_integrity import verify_copy
            print(json.dumps(verify_copy(args.feedback_id, confirm=args.confirm), ensure_ascii=False,
                indent=2, allow_nan=False))
            return 0
        if args.feedback_action == "draft":
            value = store.draft(args.source, args.file, args.by, args.reason, confirm=args.confirm)
        else:
            value = store.withdraw(args.feedback_id, args.by, args.reason, args.if_version)
        print(json.dumps({"feedback_id": value["feedback_id"], "version": value["version"],
            "state": value["state"], "grants_execution": False, "upload_authorized": False}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print("ops feedback: " + display(str(exc), 1024))
        return 2


def _read(args) -> int:
    from .task_center import snapshot
    if args.feedback_action == "show" and not FEEDBACK_PATTERN.fullmatch(args.feedback_id):
        raise ValueError("invalid feedback identity")
    view = snapshot(source_id=args.source)
    if args.feedback_action == "show":
        record = next((row for row in view["feedback"] if row["feedback_id"] == args.feedback_id), None)
        if record is None:
            print("feedback draft not found in registered bounded view")
            return 1
        print(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False))
    elif args.json:
        print(json.dumps({"feedback": view["feedback"], "diagnostics": view["diagnostics"]},
            ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for row in view["feedback"]:
            print(f"{row['feedback_id']} v{row['version']} {row['project']} {row['status']}")
            print("  " + (row["summary"] or "摘要已撤回或当前来源定义不匹配") + "；未复现、未授上传/执行权")
            matches = row["duplicate_inspection"]
            print(f"  内容元数据匹配候选 {matches['observed_matches']}；{matches['selection']}（非同根因、非完整去重）")
        for note in view["diagnostics"]:
            print("NOTE: " + note)
    return 0
