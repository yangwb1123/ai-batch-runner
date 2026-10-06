"""Local-only consent commands; shared readers expose hints, never raw prompt bodies."""
from __future__ import annotations

import json

from . import task_center
from . import task_intent_store as store
from .task_center_io import display


def add_parser(sub) -> None:
    parser = sub.add_parser("intents", help="显式授权的 prompt 历史线索；推断不是执行授权")
    actions = parser.add_subparsers(dest="intent_action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--source", default="")
    listing.add_argument("--json", action="store_true")
    showing = actions.add_parser("show")
    showing.add_argument("intent_id")
    importing = actions.add_parser("import", help="显式导入登记根内的有界 JSON/原生 JSONL；不扫描会话")
    importing.add_argument("--source", required=True)
    importing.add_argument("--goal", required=True)
    importing.add_argument("--file", required=True)
    importing.add_argument("--origin-file", default="", help="v2 范围导入必须另行显式授权原生文件重放")
    importing.add_argument("--format", choices=("normalized", "pi-session", "codex-session"), default="normalized")
    importing.add_argument("--channel", default="", help="原生导入必须显式命名授权通道")
    importing.add_argument("--leaf-id", default="", help="Pi v3 必须显式选择分支叶节点")
    importing.add_argument("--confirm", action="store_true")
    importing.add_argument("--if-version", type=int, default=None)
    _actor(importing)
    _export_parser(actions)
    revoke = actions.add_parser("revoke", help="立即停止投影，不删除审计或私有历史")
    revoke.add_argument("intent_id")
    revoke.add_argument("--if-version", type=int, required=True)
    _actor(revoke)


def _export_parser(actions) -> None:
    exporting = actions.add_parser("export", help="显式选定原生用户范围，只导出本地私有文件，不自动授权导入")
    exporting.add_argument("--source", required=True)
    exporting.add_argument("--goal", required=True)
    exporting.add_argument("--goal-version", type=int, required=True)
    exporting.add_argument("--file", required=True)
    exporting.add_argument("--format", choices=("pi-session", "codex-session"), required=True)
    exporting.add_argument("--channel", required=True)
    exporting.add_argument("--leaf-id", default="")
    exporting.add_argument("--first-user", type=int, required=True)
    exporting.add_argument("--last-user", type=int, required=True)
    exporting.add_argument("--output", required=True, help="仅 .json 文件名；写到已登记 data-dir/exports/intents")
    exporting.add_argument("--confirm", action="store_true")
    _actor(exporting)


def _export(args) -> int:
    from .task_history_export import export_history
    result = export_history(args.source, args.goal, args.file, args.format, args.channel, args.leaf_id,
                            args.first_user, args.last_user, args.output, args.by, args.reason, args.goal_version)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


def _actor(parser) -> None:
    parser.add_argument("--by", required=True, help="未认证的操作人署名")
    parser.add_argument("--reason", required=True)


def command(args, _cwd: str) -> int:
    try:
        if args.intent_action in {"list", "show"}:
            return _read(args)
        if args.intent_action in {"import", "export"} and not args.confirm:
            raise ValueError("history " + args.intent_action + " requires explicit --confirm")
        if args.intent_action == "export":
            return _export(args)
        if args.intent_action == "import":
            value = store.import_history(args.source, args.goal, args.file,
                                         args.by, args.reason, args.if_version,
                                         format=args.format, channel=args.channel, leaf=args.leaf_id,
                                         origin_file=args.origin_file)
        else:
            value = store.revoke(args.intent_id, args.by, args.reason, args.if_version)
        print(json.dumps({"intent_id": value["intent_id"], "version": value["version"],
                          "authorized": value["authorized"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print("ops intents: " + display(str(exc), 1024))
        return 2


def _read(args) -> int:
    view = task_center.snapshot(source_id=getattr(args, "source", ""))
    if args.intent_action == "show":
        row = next((row for row in view["intents"] if row["intent_id"] == args.intent_id), None)
        if row is None:
            print("prompt history not found in the registered bounded view")
            return 1
        print(json.dumps(row, ensure_ascii=False, indent=2, allow_nan=False))
    elif args.json:
        print(json.dumps({"intents": view["intents"], "diagnostics": view["diagnostics"]},
                         ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for row in view["intents"]:
            print(f"{row['intent_id']} v{row['version']} {row['channel']} {row['status']} "
                  f"{row['coverage']['messages']} messages (角色为导出声明)")
        for note in view["diagnostics"]:
            print("NOTE: " + note)
    return 0
