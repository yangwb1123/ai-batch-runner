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
            "note": "被拒产物有界摘录保留在 .pi-batch/rejected/ 供修复诊断；"
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
        return [], "no --log-file log found under " + str(Path(cwd) / "logs")
    lines, error = log_tail(target, tail=tail)
    return lines, error


def _default_log_file(cwd: str) -> Optional[str]:
    logs_dir = Path(cwd) / "logs"
    if not logs_dir.is_dir():
        return None
    candidates = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    return str(candidates[0]) if candidates else None


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

def _breaker_label(pb_state: dict) -> str:
    if pb_state.get("active"):
        return (f"ACTIVE cooldown {pb_state.get('cooldown_remaining_s', 0)}s "
                f"(step {pb_state.get('step', 0)})")
    return (f"ok (streak {pb_state.get('streak', 0)}/"
            f"{pb_state.get('threshold', '-')})")


def _runs_table(rows: list, esc, status_class: dict) -> str:
    return "\n".join(
        f"<tr class='{status_class.get(row['status'], '')}'><td>{esc(row['time'])}"
        f"</td><td>{esc(row['agent'])}</td><td>{esc(row['status'])}</td>"
        f"<td>#{row['attempt']}</td><td>{esc(row['task'])}</td>"
        f"<td>{esc('; '.join(row['outputs']))}</td></tr>"
        for row in rows) or "<tr><td colspan=6>no runs yet</td></tr>"


def report_html(data_dir: str, cwd: str) -> str:
    summary = overview(data_dir)
    rows = runs(data_dir, limit=50)
    log_lines, _log_error = _recent_log_lines(cwd, tail=30)
    status_class = {"succeeded": "ok", "failed": "bad", "rejected": "warn",
                    "cancelled": "warn"}

    def esc(value: object) -> str:
        return html_mod.escape(str(value), quote=True)

    rows_html = _runs_table(rows, esc, status_class)
    log_escaped = esc("\n".join(log_lines) or
                       _log_error or "(no --log-file log found)")
    pb_label = _breaker_label(summary.get("provider_breaker") or {})
    statuses = " ".join(
        f"<span class='chip {status_class.get(k, '')}'>{esc(k)}={v}</span>"
        for k, v in summary["status_counts"].items()) or "—"
    agents = " ".join(
        f"<span class='chip'>{esc(k)}={v}</span>"
        for k, v in summary["by_agent"].items()) or "—"
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pi-batch ops · {esc(Path(cwd).name)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}}
h1{{font-size:1.3rem}} h2{{font-size:1rem;margin-top:2rem}}
.chip{{display:inline-block;margin:.15rem;padding:.1rem .5rem;border-radius:999px;
background:#f0f0f0;font-size:.85rem}} .ok{{color:#0a7d2f}}.bad{{color:#c0392b}}
.warn{{color:#b26a00}} table{{border-collapse:collapse;width:100%;font-size:.85rem}}
td,th{{border:1px solid #ddd;padding:.3rem .5rem;text-align:left;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:340px}}
tr.ok td:first-child{{}} tr.bad{{background:#fff3f3}} tr.warn{{background:#fff8e8}}
.meta{{color:#666;font-size:.85rem}}
pre.log{{background:#f6f6f6;padding:.5rem;overflow-x:auto;font-size:.75rem;
max-height:24em}}
</style></head><body>
<h1>pi-batch · 运营控制台 · {esc(Path(cwd).name)}</h1>
<p class="meta">生成于 {esc(summary['generated_at'])} · 共 {summary['total']} 条 run
· 被拒 {summary['rejected_count']}（自我修复诊断见 {esc(summary['self_healing']['rejected_artifacts_in'])}）</p>
<p class="meta">Provider breaker: {pb_label}</p>
<h2>状态分布</h2><div>{statuses}</div>
<h2>按 Agent</h2><div>{agents}</div>
<h2>最近 Runs（含重试 attempt）</h2>
<table><thead><tr><th>时间</th><th>agent</th><th>状态</th><th>#</th><th>任务</th>
<th>产物</th></tr></thead><tbody>{rows_html}</tbody></table>
<h2>运营日志（尾部，ops log / api/log 可过滤）</h2>
<pre class="log">{log_escaped}</pre>
<p class="meta">刷新本页即可看到最新状态（静态快照；实时推送为后续路线）。</p>
</body></html>"""


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


def _cmd_report(args, cwd: str) -> int:
    out = Path(args.html)
    if not out.is_absolute():
        out = Path(cwd) / out
    if Path(cwd).resolve() not in out.resolve().parents:
        print(f"report path must stay inside cwd: {out}")
        return 2
    out.write_text(report_html(_data_dir(cwd), cwd), encoding="utf-8")
    print(f"report written: {out} (open in a browser / phone)")
    return 0


def ops_main(argv: Optional[list[str]] = None) -> int:
    """运营控制台：任务/日志/message 可视化。"""
    parser = argparse.ArgumentParser(
        prog="pi-batch ops", description="运营控制台：任务/日志/message 可视化")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("overview", help="账本统计")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("runs", help="最近 run 明细")
    p.add_argument("--agent", default="")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("log", help="运营日志尾部/过滤")
    p.add_argument("--file", default="")
    p.add_argument("--tail", type=int, default=80)
    p.add_argument("--grep", default="")
    p = sub.add_parser("messages", help="session message 索引最近记录")
    p.add_argument("--limit", type=int, default=5)
    p = sub.add_parser("report", help="生成自包含 HTML 监控页")
    p.add_argument("--html", default=OPS_REPORT_NAME,
                   help="输出路径（默认 ./ops-report.html，须在 cwd 内）")
    p = sub.add_parser("serve", help="只读 HTTP 监控服务（web/app 实时查看）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8731)
    p.add_argument("--token", default="")
    args = parser.parse_args(argv)
    cwd = os.getcwd()
    if args.command == "serve":
        from .ops_serve import serve_main
        return serve_main([f"--host={args.host}", f"--port={args.port}",
                           f"--token={args.token}"])
    handlers = {
        "overview": _cmd_overview,
        "runs": _cmd_runs,
        "log": _cmd_log,
        "messages": _cmd_messages,
        "report": _cmd_report,
    }
    return handlers[args.command](args, cwd)
