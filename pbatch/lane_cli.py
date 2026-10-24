"""``pi-batch lanes``: plan-check, dry-run and one supervised round of lanes.

The command is the operator entry point for the machinery in
:mod:`pbatch.lane_loop` (anchored window, restart policy) and
:mod:`pbatch.lane_run` (concurrent worktrees, serial gated merge). It never
chooses models, prompts or budgets by itself: the plan file declares the lane
argv, and the window is declared on the command line once.

``--dry-run`` resolves and validates everything, prints the anchored window and
the exact lane argv, and exits without creating a worktree, running a command or
merging.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import time
from pathlib import Path
from typing import Optional

from . import lane_loop, lane_run, session_merge


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-batch lanes",
                                     description="并发隔离泳道 + 串行门禁合并 + 监督窗口")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="只读：锚定窗口、轮次与停止原因")
    status.add_argument("--root", default=".", help="仓库根（默认当前目录）")
    status.add_argument("--json", action="store_true")
    resume = commands.add_parser("resume", help="解除可恢复停止（backlog 耗尽/操作者暂停）")
    resume.add_argument("--root", default=".", help="仓库根（默认当前目录）")
    resume.add_argument("--json", action="store_true")
    run = commands.add_parser("run", help="执行一轮（或按窗口循环）泳道")
    run.add_argument("--plan", required=True, help="lane 计划 JSON/YAML")
    run.add_argument("--root", default=".", help="仓库根（默认当前目录）")
    run.add_argument("--hours", type=float, default=0,
                     help="锚定的窗口小时数（首次运行必填；重启后沿用原值）")
    run.add_argument("--invocations", type=int, default=0,
                     help="窗口内允许的 agent 调用总数（首次运行必填）")
    run.add_argument("--jobs", type=int, default=2)
    run.add_argument("--base", default="main")
    run.add_argument("--max-rounds", type=int, default=1,
                     help="本次最多执行几轮（0 = 按窗口循环，直到策略停止）")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--json", action="store_true")
    return parser


def _load_plan(path: str, root: Path) -> tuple[list, str]:
    source = Path(path).expanduser()
    if not source.is_file() or source.is_symlink():
        raise ValueError("lane plan must be a regular file: " + str(source))
    raw = source.read_text(encoding="utf-8")
    if source.suffix.lower() in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("lane plan must be a mapping")
    gate = data.get("gate", "")
    rows = data.get("lanes")
    if not isinstance(rows, list):
        raise ValueError("lane plan needs a 'lanes' list")
    lanes = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("lane plan entry %d must be a mapping" % index)
        extra = set(row) - {"id", "branch", "worktree", "command", "timeout", "scope"}
        if extra:
            raise ValueError("lane plan entry %d has unknown keys: %s"
                             % (index, ", ".join(sorted(extra))))
        command = row.get("command")
        if isinstance(command, str):
            command = shlex.split(command)
        lanes.append(lane_run.Lane(
            lane_id=str(row.get("id", "")),
            branch=str(row.get("branch", "")),
            worktree=Path(str(row.get("worktree", ""))).expanduser(),
            command=tuple(command or ()),
            timeout=int(row.get("timeout", lane_run.LANE_TIMEOUT_DEFAULT)),
            scope=tuple(row.get("scope") or ()),
        ))
    return lanes, str(gate)


def _window_for(args) -> lane_loop.Window:
    current = lane_loop.status(Path(args.root))
    if current["anchored"]:
        return lane_loop.start_window(Path(args.root),
                                      hours=(current["deadline_at"] - current["started_at"]) / 3600.0,
                                      invocations=current["budget"])
    if not args.hours or not args.invocations:
        raise ValueError("first run requires --hours and --invocations; "
                         "a restarted supervisor reuses the anchored window")
    return lane_loop.start_window(Path(args.root), hours=args.hours,
                                  invocations=args.invocations)


def _classify(receipt: dict, lanes: list) -> str:
    """Separate "work was never produced" from "work failed a gate".

    A lane whose command died (provider/network/timeout) produced nothing to
    judge, so the round is a liveness failure and may be retried with backoff.
    A lane that produced commits and then failed its scope or gate is
    deterministic: retrying it only hides the failure.
    """
    merged = set(receipt.get("merged") or [])
    blocked = set(receipt.get("blocked") or [])
    if len(merged) == len(lanes) and not blocked:
        return lane_loop.COMPLETED
    statuses = {row.get("status") for row in receipt.get("lanes") or []}
    if statuses and statuses <= {"lane_failed", "lane_timeout"}:
        return lane_loop.CRASHED
    if blocked:
        return lane_loop.GATE_FAILED
    return lane_loop.BLOCKED


def _print_status(root: Path, as_json: bool) -> int:
    value = lane_loop.status(root)
    rounds = lane_run.read_rounds(root, limit=1)
    value["last_round"] = ({key: rounds[-1].get(key)
                            for key in ("at", "merged", "blocked", "head")}
                           if rounds else None)
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print("anchored: %s  remaining: %ss  budget: %s  consumed: %s  rounds: %s  restarts: %s"
              % (value["anchored"], value["remaining_seconds"], value["budget"],
                 value["consumed"], value["rounds"], value["restarts"]))
        print("stopped: %s" % (value["stopped"] or "-"))
        print("last_round: %s" % json.dumps(value["last_round"], ensure_ascii=False))
    return 0


def _resolved(args, root: Path):
    lanes, gate = _load_plan(args.plan, root)
    lane_run.validate_plan(root, lanes, gate)
    window = _window_for(args)
    lane_run.preflight(root, lanes)
    return lanes, gate, window


def _print_dry_run(args, lanes, gate, window) -> None:
    print(json.dumps({"window": {"deadline_at": window.deadline_at,
                                 "invocation_budget": window.invocation_budget},
                      "gate": gate, "jobs": args.jobs, "base": args.base,
                      "lanes": [{"id": lane.lane_id, "branch": lane.branch,
                                 "worktree": str(lane.worktree),
                                 "command": list(lane.command)}
                                for lane in lanes]}, ensure_ascii=False, indent=2))


def _run_rounds(args, root: Path, lanes: list, gate: str) -> int:
    rounds, decision = 0, None
    while args.max_rounds == 0 or rounds < args.max_rounds:
        if lane_loop.status(root)["stopped"]:
            break
        receipt = lane_run.run_round(root, lanes, gate=gate, jobs=args.jobs, base=args.base)
        rounds += 1
        # Budget counts every launched lane: a failed lane already spent agent work.
        invocations = len(receipt["lanes"])
        decision = lane_loop.decide(root, classification=_classify(receipt, lanes),
                                    invocations=invocations)
        if args.json:
            print(json.dumps({"round": rounds, "receipt": receipt,
                              "decision": decision.action, "reason": decision.reason},
                             ensure_ascii=False))
        else:
            print("round %d: merged=%s blocked=%s -> %s (%s)"
                  % (rounds, receipt["merged"], receipt["blocked"],
                     decision.action, decision.reason))
        if decision.action != lane_loop.RESTART:
            break
        if decision.backoff_seconds:
            time.sleep(min(decision.backoff_seconds, 600.0))
    return 0 if decision is None or decision.action != lane_loop.STOP else 4


def run_main(argv: Optional[list] = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if args.command == "status":
        return _print_status(root, args.json)
    if args.command == "resume":
        try:
            lifted = lane_loop.resume(root)
        except (lane_loop.LaneLedgerError, OSError, ValueError) as exc:
            print("lanes: %s" % exc)
            return 2
        print(json.dumps({"resumed": lifted}) if args.json else "resumed: %s" % lifted)
        return 0
    try:
        lanes, gate, window = _resolved(args, root)
    except (OSError, ValueError) as exc:
        print("lanes: %s" % exc)
        return 2
    if window.expired(time.time()):
        print("lanes: anchored window already expired; refusing to start")
        return 3
    if args.dry_run:
        _print_dry_run(args, lanes, gate, window)
        return 0
    return _run_rounds(args, root, lanes, gate)

lanes_main = run_main
