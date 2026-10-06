"""Explicit local contract/attestation commands; HTTP remains observation only."""
from __future__ import annotations

import json
from pathlib import Path

from . import task_center
from . import task_goal_store as store
from .task_center_io import display, read_object


def add_parser(sub) -> None:
    parser = sub.add_parser("goals", help="目标契约、run 关联与证据绑定；不是执行授权")
    commands = parser.add_subparsers(dest="goal_action", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--source", default="")
    listing.add_argument("--json", action="store_true")
    showing = commands.add_parser("show")
    showing.add_argument("goal_id")
    defining = commands.add_parser("define")
    defining.add_argument("--source", required=True)
    defining.add_argument("--file", required=True, help="bounded JSON goal contract")
    defining.add_argument("--confirm", action="store_true", help="explicit metadata confirmation")
    defining.add_argument("--if-version", type=int, default=None)
    _actor_arguments(defining)
    binding = commands.add_parser("bind")
    binding.add_argument("goal_id")
    binding.add_argument("--run-id", required=True)
    binding.add_argument("--if-version", type=int, required=True)
    _actor_arguments(binding)
    receipt = commands.add_parser("attest")
    receipt.add_argument("goal_id")
    receipt.add_argument("--criterion", required=True)
    receipt.add_argument("--decision", choices=("accepted", "rejected"), required=True)
    receipt.add_argument("--evidence", default="")
    receipt.add_argument("--if-version", type=int, required=True)
    _actor_arguments(receipt)


def _actor_arguments(parser) -> None:
    parser.add_argument("--by", required=True, help="recorded operator label, not authenticated identity")
    parser.add_argument("--reason", required=True)


def command(args, _cwd: str) -> int:
    try:
        if args.goal_action in {"list", "show"}:
            return _read(args)
        if args.goal_action == "define":
            if not args.confirm:
                raise ValueError("goal definition needs explicit --confirm")
            manifest = read_object(Path(args.file), 64 * 1024)
            if manifest is None:
                raise ValueError("goal contract file not found")
            value = store.define(args.source, manifest, args.by, args.reason, args.if_version)
        elif args.goal_action == "bind":
            value = store.bind(args.goal_id, args.run_id, args.by, args.reason, args.if_version)
        else:
            value = store.attest(args.goal_id, args.criterion, args.decision, args.evidence,
                                 args.by, args.reason, args.if_version)
        print(json.dumps({"goal_id": value["goal_key"], "version": value["version"],
                          "contract_revision": value["contract_revision"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print("ops goals: " + display(str(exc), 1024))
        return 2


def _read(args) -> int:
    view = task_center.snapshot(source_id=getattr(args, "source", ""))
    if args.goal_action == "show":
        selected = next((goal for goal in view["goals"] if goal["goal_id"] == args.goal_id), None)
        if selected is None:
            print("goal not found in the registered bounded view")
            return 1
        print(json.dumps(selected, ensure_ascii=False, indent=2, allow_nan=False))
    elif args.json:
        print(json.dumps({"goals": view["goals"], "diagnostics": view["diagnostics"]},
                         ensure_ascii=False, indent=2, allow_nan=False))
    else:
        for goal in view["goals"]:
            print(f"{goal['goal_id']} v{goal['version']} {goal['project']} {goal['acceptance']}")
            print("  " + goal["statement"])
            accounting = goal["consumption"]
            from .task_center_cli import _money, _shown
            print(f"  窗口 Token={_shown(accounting['usage']['total_tokens'])}；"
                  f"{_money(accounting['billing'])}；独立验收未验证")
        for note in view["diagnostics"]:
            print("NOTE: " + note)
    return 0
