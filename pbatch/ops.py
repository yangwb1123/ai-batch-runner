"""运营控制台（T15）：7×24 任务的自我修复/优化过程可视化。

`pi-batch ops` 子命令组把分散的事实源（共享 run 账本、轮转运营日志、
session message 索引）投影为可读视图，并生成自包含 HTML 监控页——
任务的执行、失败、重试、被拒、产物全程可见，浏览器/移动端打开即用。

子命令：
  ops overview            账本统计（状态分布/按 agent/被拒计数）
  ops runs [--agent]      最近 run 明细（含 attempt/原因）
  ops log [--tail|--grep] 运营日志尾部/过滤（有界）
  ops messages [--find]   session message 索引查询
  ops report --html       自包含 HTML 监控页（web/app 可视化入口）
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config
from .agent_ledger import query as ledger_query
from .task_center_cli import add_parsers as add_task_parsers, sources_command, tasks_command
from .task_goal_cli import command as goals_command
from .task_intent_cli import command as intents_command

OPS_REPORT_NAME = "ops-report.html"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _brief(text: str, width: int = 80) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


# ── overview ──────────────────────────────────────────────────────────

def overview(data_dir: str) -> dict:
    rows = ledger_query(data_dir)
    statuses = Counter(str(r.get("status", "?")) for r in rows)
    agents = Counter(str(r.get("agent", "?")) for r in rows)
    rejected = statuses.get("rejected", 0)
    failed = statuses.get("failed", 0)
    breaker_status: dict = {}
    try:
        from .provider_breaker import breaker_state
        breaker_status = breaker_state(data_dir)
    except Exception:
        pass
    return {
        "generated_at": _utc_now(),
        "total": len(rows),
        "status_counts": dict(statuses),
        "by_agent": dict(agents),
        "rejected_count": rejected,
        "failed_count": failed,
        "provider_breaker": breaker_status,
        "self_healing": {
            "rejected_artifacts_in": str(Path(data_dir) / "rejected"),
            "note": "被拒产物有界摘录保留在用户级 runtime/rejected/ 供修复诊断；"
                    "--retries/--max-rounds/gate-fix 负责自动修复",
        },
    }


# ── runs ──────────────────────────────────────────────────────────────

def runs(data_dir: str, agent: str = "", limit: int = 30) -> list[dict]:
    rows = ledger_query(data_dir, agent=agent, limit=limit)
    return [{
        "time": str(r.get("ts", ""))[:19],
        "agent": str(r.get("agent", "")),
        "status": str(r.get("status", "")),
        "attempt": int(r.get("attempt", 0) or 0),
        "task": _brief(r.get("task_brief", ""), 70),
        "outputs": [str(o) for o in (r.get("outputs") or [])][:4],
        "elapsed_ms": int(r.get("elapsed_ms", 0) or 0),
        "exit_code": int(r.get("exit_code", 0) or 0),
    } for r in rows]


# ── log ───────────────────────────────────────────────────────────────

def _recent_log_lines(cwd: str, tail: int = 30) -> tuple[list, str]:
    target = _default_log_file(cwd) or ""
    if not target:
        return [], "no --log-file log found under " + str(config.data_dir(cwd) / "logs")
    lines, error = log_tail(target, tail=tail)
    return lines, error


def _default_log_file(cwd: str) -> Optional[str]:
    """Find the newest private log, with a read-only legacy fallback."""
    directories = [config.data_dir(cwd) / "logs", Path(cwd) / "logs"]
    for logs_dir in directories:
        if not logs_dir.is_dir():
            continue
        candidates = sorted(logs_dir.glob("*.log"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return str(candidates[0])
    return None


def log_tail(file: str, tail: int = 80, grep: str = "") -> tuple[list[str], str]:
    path = Path(file)
    if not path.exists():
        return [], f"log file not found: {file}"
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - 256 * 1024))
        text = f.read().decode("utf-8", errors="replace")
    lines = text.splitlines()
    if grep:
        pattern = re.compile(re.escape(grep), re.IGNORECASE)
        lines = [line for line in lines if pattern.search(line)]
    return lines[-tail:], ""


# ── messages ──────────────────────────────────────────────────────────

def message_recent(data_dir: str, limit: int = 5) -> list[dict]:
    """Session message 索引最近记录（封装 memory recent，只读）。"""
    try:
        from .memory import recent_records
        return [{"time": str(r.get("ts", ""))[:19], "session": str(r.get("sid", "")),
                 "brief": _brief(r.get("prompt", "") or r.get("summary", ""), 70)}
                for r in recent_records(data_dir, limit)]
    except Exception as exc:
        return [{"error": f"message index unavailable: {exc}"}]


# ── report（HTML 监控页）──────────────────────────────────────────────

from .ops_report import _breaker_label, report_html


# ── CLI ───────────────────────────────────────────────────────────────

def _data_dir(cwd: str) -> str:
    return str(config.data_dir(cwd))


def _cmd_overview(args, cwd: str) -> int:
    payload = overview(_data_dir(cwd))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"total runs: {payload['total']}")
        for status, count in payload["status_counts"].items():
            print(f"  {status:<10} {count}")
        print(f"self-healing: 被拒 {payload['rejected_count']} 条，"
              f"诊断见 {payload['self_healing']['rejected_artifacts_in']}")
    return 0


def _cmd_instances(args, cwd: str) -> int:
    """Progress per workspace runtime dir, flagged only on evidence."""
    from .instance_view import instance_rows
    payload = instance_rows(stale_seconds=args.stale_seconds)
    notified = None
    if args.notify:
        from . import instance_alerts
        rows = payload["workspaces"]
        notified = instance_alerts.notify(
            rows, url=args.webhook_url, repeat_seconds=args.repeat_seconds)
    if args.json:
        print(json.dumps({"view": payload, "notified": notified},
                         ensure_ascii=False, indent=2))
        return 0
    if payload["unavailable"]:
        print(f"runtime home unavailable: {payload['unavailable']}")
        return 0
    rows = payload["workspaces"]
    if not rows:
        print("no workspace runtime directories found")
        return 0
    print(f"{'workspace':<28} {'state':<10} {'idle':>7} {'pass':>5} "
          f"{'fail':>5} {'cited':>6}  flag")
    for row in rows:
        idle = row["idle_seconds"]
        print(f"{row['workspace'][:28]:<28} "
              f"{str(row['session_state'] or '-'):<10} "
              f"{(f'{idle}s' if idle is not None else '-'):>7} "
              f"{row['passes']:>5} {row['failures']:>5} {row['cited_stages']:>6}  "
              f"{row['flag'] or '-'}")
        if row["flag_reason"]:
            print(f"    {row['flag_reason']}")
    print(f"\nflag rule: {payload['flag_rule']}")
    if notified is not None:
        target = args.webhook_url or "stdout"
        print(f"alerts: due={notified['due']} sent={notified['sent']} "
              f"resolved={notified['resolved']} via {target}")
    return 0


def _cmd_runs(args, cwd: str) -> int:
    rows = runs(_data_dir(cwd), agent=args.agent, limit=args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        if not rows:
            print("no runs")
        for row in rows:
            print(f"{row['time']} {row['agent']:<12} {row['status']:<10} "
                  f"#{row['attempt']} {row['task']}")
    return 0


def _cmd_log(args, cwd: str) -> int:
    target = args.file or _default_log_file(cwd) or ""
    if not target:
        print("no log file found; pass --file (e.g. --log-file output)")
        return 1
    lines, error = log_tail(target, tail=args.tail, grep=args.grep)
    if error:
        print(error)
        return 1
    print(f"# {target} ({len(lines)} lines)")
    for line in lines:
        print(line)
    return 0


def _cmd_messages(args, cwd: str) -> int:
    for row in message_recent(_data_dir(cwd), args.limit):
        print(f"{row.get('time', '?'):<20} {row.get('brief', '')}")
    return 0


def _breaker_provider(provider: str) -> tuple[str, bool]:
    value = str(provider or "").strip()
    if not value:
        return "", True
    from . import provider_breaker
    identity = getattr(provider_breaker, "breaker_identity", None)
    if not callable(identity):
        return value, False
    return identity(value, ""), True


def _cmd_breaker(args, cwd: str) -> int:
    from . import provider_breaker
    data_dir = _data_dir(cwd)
    provider, supports_partition = _breaker_provider(args.provider)
    if args.provider and not supports_partition:
        print("provider-specific breaker control is unavailable in this version")
        return 2
    if args.reset:
        try:
            provider_breaker.register_success(data_dir, provider=provider)
        except TypeError:  # legacy aggregate API
            provider_breaker.register_success(data_dir)
        try:
            from .metering import record_event
            record_event("provider_breaker_manual_reset", provider or "*",
                         "operator requested breaker reset", data_dir)
        except Exception:
            pass
        config.log.warning("PROVIDER BREAKER MANUAL RESET [%s]", provider or "all")
    try:
        state = (provider_breaker.breaker_state(data_dir, provider=provider)
                 if provider else provider_breaker.breaker_state(data_dir))
    except TypeError:  # legacy aggregate API
        state = provider_breaker.breaker_state(data_dir)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        scope = f" [{provider}]" if provider else ""
        print(f"provider breaker{scope}: {_breaker_label(state)}")
    return 0


def _cmd_report(args, cwd: str) -> int:
    out = Path(args.html)
    if not out.is_absolute():
        out = Path(cwd) / out
    resolved = out.resolve()
    allowed = (Path(cwd).resolve(), config.data_dir(cwd).resolve())
    if not any(resolved == root or root in resolved.parents for root in allowed):
        print(f"report path must stay inside cwd or runtime directory: {out}")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_html(_data_dir(cwd), cwd), encoding="utf-8")
    print(f"report written: {out} (open in a browser / phone)")
    return 0


def _add_serve_args(parser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--token", default="")
    parser.add_argument("--token-file", default="")
    parser.add_argument("--control", action="store_true")
    parser.add_argument("--global", dest="global_tasks", action="store_true",
                        help="全局任务中心（只读，要求 token）")
    parser.add_argument("--agent", default=config.AGENT_BIN)
    parser.add_argument("--model", default=config.AGENT_DEFAULT_MODEL)
    parser.add_argument("--provider", default="")
    parser.add_argument("--runtime", default="auto",
                        choices=("auto", "pi", "pi-rpc", "codex", "codex-exec", "legacy"))
    parser.add_argument("--control-timeout", type=int, default=900)
    parser.add_argument("--max-sessions", type=int, default=4)
    parser.add_argument("--tls-cert", default="")
    parser.add_argument("--tls-key", default="")


def _serve_argv(args) -> list[str]:
    values = [f"--host={args.host}", f"--port={args.port}",
              f"--token={args.token}", f"--token-file={args.token_file}",
              f"--agent={args.agent}", f"--model={args.model}",
              f"--provider={args.provider}", f"--runtime={args.runtime}",
              f"--control-timeout={args.control_timeout}",
              f"--max-sessions={args.max_sessions}",
              f"--tls-cert={args.tls_cert}", f"--tls-key={args.tls_key}"]
    return (values + (["--control"] if args.control else [])
            + (["--global"] if args.global_tasks else []))


def _build_parser() -> argparse.ArgumentParser:
    """Subcommand surface, split out to keep ops_main within budget."""
    parser = argparse.ArgumentParser(
        prog="pi-batch ops", description="运营控制台：任务/日志/message 可视化")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("overview", help="账本统计")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("runs", help="最近 run 明细")
    p.add_argument("--agent", default="")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("instances", help="各工作空间实例进度（只读，不调度）")
    p.add_argument("--stale-seconds", type=int, default=900,
                   help="判定「静默未完成」的秒数（默认 900）")
    p.add_argument("--notify", action="store_true",
                   help="向 --webhook-url 推送当前告警（按 --repeat-seconds 去重）")
    p.add_argument("--webhook-url", default="",
                   help="告警投递地址；不传则只统计不外发")
    p.add_argument("--repeat-seconds", type=int, default=3600,
                   help="同一告警的最短重复间隔（默认 3600）")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("log", help="运营日志尾部/过滤")
    p.add_argument("--file", default="")
    p.add_argument("--tail", type=int, default=80)
    p.add_argument("--grep", default="")
    p = sub.add_parser("messages", help="session message 索引最近记录")
    p.add_argument("--limit", type=int, default=5)
    p = sub.add_parser("report", help="生成自包含 HTML 监控页")
    p.add_argument(
        "--html", default="",
        help="输出路径（默认用户级运行目录，不写入项目）")
    p = sub.add_parser("breaker", help="查看或显式复位 provider 熔断")
    p.add_argument("--provider", default="", help="provider 名称（不传则全部）")
    p.add_argument("--reset", action="store_true",
                   help="人工确认 provider 已恢复后清除熔断状态")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("serve", help="手机/浏览器监控；--control 开启受保护指令")
    _add_serve_args(p)
    add_task_parsers(sub)
    return parser


def ops_main(argv: Optional[list[str]] = None) -> int:
    """运营控制台：任务/日志/message 可视化。"""
    args = _build_parser().parse_args(argv)
    cwd = os.getcwd()
    if args.command == "report" and not args.html:
        args.html = str(config.data_dir(cwd) / "ops" / OPS_REPORT_NAME)
    if args.command == "serve":
        from .ops_serve import serve_main
        return serve_main(_serve_argv(args))
    handlers = {
        "overview": _cmd_overview,
        "runs": _cmd_runs,
        "instances": _cmd_instances,
        "log": _cmd_log,
        "messages": _cmd_messages,
        "report": _cmd_report,
        "breaker": _cmd_breaker,
        "sources": sources_command,
        "tasks": tasks_command,
        "goals": goals_command,
        "intents": intents_command,
    }
    return handlers[args.command](args, cwd)
