"""CLI surfaces for source registration and the shared read-only task view."""
from __future__ import annotations

import json
import math
import time

from . import task_center, task_sources


def add_parsers(sub) -> None:
    parser = sub.add_parser("tasks", help="全局任务进度、成本与产物（只读）")
    parser.add_argument("--source", default="", help="source ID or registered name")
    parser.add_argument("--id", default="", help="inspect one recent task")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--follow", action="store_true", help="poll; JSON mode emits JSONL snapshots")
    parser.add_argument("--interval", type=float, default=5.0)
    parser = sub.add_parser("sources", help="登记自定义 runtime；不移动、不删除项目数据")
    actions = parser.add_subparsers(dest="source_action", required=True)
    actions.add_parser("list").add_argument("--json", action="store_true")
    add = actions.add_parser("add")
    add.add_argument("name")
    add.add_argument("--data-dir", required=True)
    add.add_argument("--workspace", default="", help="explicit root for relative output paths")
    add.add_argument("--campaign-state", default="")
    add.add_argument("--events-file", default="")
    remove = actions.add_parser("remove", help="仅移除登记，不清理任何目录")
    remove.add_argument("source_id")
    from .task_goal_cli import add_parser
    add_parser(sub)
    from .task_intent_cli import add_parser as add_intents
    add_intents(sub)
    from .task_repair_cli import add_parser as add_repairs
    add_repairs(sub)
    from .task_retention_cli import add_parser as add_retention
    add_retention(sub)
    from .task_feedback_cli import add_parser as add_feedback
    add_feedback(sub)


def sources_command(args, _cwd: str) -> int:
    try:
        if args.source_action == "add":
            item = task_sources.source(args.name, args.data_dir, args.workspace,
                                       args.campaign_state, args.events_file)
            print(json.dumps(task_sources.register_source(item), ensure_ascii=False))
        elif args.source_action == "remove":
            print(json.dumps(task_sources.unregister_source(args.source_id)))
        else:
            rows, notes = task_sources.discover_sources()
            if args.json:
                print(json.dumps({"sources": rows, "diagnostics": notes}, ensure_ascii=False))
            else:
                for row in rows:
                    print(f"{row['source_id']}  {row['name']}  {row['data_dir']}")
                for note in notes:
                    print(f"NOTE: {note}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"ops sources: {exc}")
        return 2


def tasks_command(args, _cwd: str) -> int:
    if not math.isfinite(args.interval) or args.interval < 1:
        print("ops tasks: interval must be finite and at least one second")
        return 2
    try:
        while True:
            view = task_center.snapshot(args.limit, args.source)
            if args.id:
                selected = next((row for row in view["tasks"] if row["task_id"] == args.id), None)
                if selected is None:
                    print("task not found in the bounded recent window")
                    return 1
                print(json.dumps(selected, ensure_ascii=False, indent=None if args.follow else 2,
                                 allow_nan=False))
            elif args.json:
                print(json.dumps(view, ensure_ascii=False, indent=None if args.follow else 2,
                                 allow_nan=False), flush=True)
            else:
                print(format_tasks(view), flush=True)
            if not args.follow:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print(f"ops tasks: {exc}")
        return 2


def _shown(value: object) -> str:
    return "未追踪" if value is None else str(round(value, 2) if isinstance(value, float) else value)


def format_tasks(view: dict) -> str:
    lines = ["PROJECT | TASK | OBSERVED STATUS | WALL(s) | TOKENS | COST | OUTPUT FILES"]
    for task in view["tasks"]:
        accounting, delivery = task.get("consumption") or task, task["delivery"]
        usage = accounting["usage"]
        lines.append(f"{task['project']} | {task['title'][:64]} | {task['status']} "
                     f"({task['phase']}) | {_shown(task['time']['elapsed_wall_seconds'])} | "
                     f"{_shown(usage['total_tokens'])} ({usage['token_coverage']}) | "
                     f"{_money(accounting['billing'])} | "
                     f"{_shown(delivery['present_files'])}/{_shown(delivery['declared_files'])}")
        lines.append(f"  {task['task_id']}  source={task['source_id']}")
        lines.append(f"  CLI 完成观测={_shown(usage['calls'])}；provider 请求观测="
                     f"{_shown(accounting['billing']['provider_requests']['observed_total'])}；非全程账单")
        if task["goal_links"]:
            lines.append("  goals=" + ", ".join(link["goal_id"] + ":" + link["acceptance"]
                         for link in task["goal_links"]))
        if task["flow"]["next_stage"]:
            lines.append(f"  next={task['flow']['next_stage']} ({task['flow']['next_status']}; 非执行授权)")
        if task["forecast"]["duration_range_seconds"]:
            lines.append("  历史耗时先验=" + str(task["forecast"]["duration_range_seconds"]) + " 秒（非实时 ETA）")
        if task["analysis"]["latest_reason"]:
            lines.append("  " + task["analysis"]["latest_reason"])
        lines.extend(_repair_lines(task))
        if task.get("repair_links"):
            lines.append("  登记恢复请求（未授执行权）=" + ", ".join(task["repair_links"]))
    for process in view["activity"]["processes"]:
        lines.append(f"ACTIVITY: pid={process['pid']} {process['presence']} "
                     f"{process['workspace']} (进程活动不等于任务进展)")
    lines.extend("NOTE: " + note for note in
                 [*view["diagnostics"], *view["activity"]["diagnostics"]])
    lines.append("有界窗口观测；产物存在/执行成功不代表目标已验收；历史耗时先验不是实时 ETA 或执行授权。")
    return "\n".join(lines)


def _repair_lines(task: dict) -> list[str]:
    preview = task.get("repair_preview")
    if not preview or preview["applicability"] != "failure_observation":
        return []
    stage = preview["candidate_stage"]
    target = "；定位阶段=" + stage["label"] + " [" + stage["stage_key"] + "]" if stage else "；阶段未选定"
    return ["  恢复预览（只读、非重启指令）=" + preview["inspection_hint"] + target,
            "  尚待检查=" + ", ".join(preview["blockers"])]


def _money(billing: dict) -> str:
    values = [f"{_shown(row['reported_amount'])} {row['currency']}" for row in billing["currency_totals"]]
    return ("; ".join(values) or "币种/费用未追踪") + " (" + billing["financial_coverage"] + ")"
