"""Cost metering (R6 / T7) and budget cap (R7 / T8).

Metering: after each task, an append-only JSONL event is written to
--events-file (task_finish / task_fail / gate_reject / budget_cap /
stall_stop) with the per-call usage diff read from the pi session file
(spike-validated JSON pointers; a missing/unparseable session degrades to
`usage: null`, never a crash). An optional --webhook URL receives the same
events non-blockingly (5s timeout, failures logged only).

Budget: --budget-max N caps agent invocations per run (exit 3); a stale
session or a webhook outage never breaks the runner.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import log

# module-level config, set by cli.main() from CLI flags
EVENTS_FILE: str = ""
WEBHOOK_URL: str = ""
INVOCATIONS: int = 0
BUDGET_MAX: int = 0          # 0 = unlimited
DAILY_BUDGET: int = 0        # 0 = unlimited
_DAILY_STATE: str = ""       # path of the daily counter state file
BUDGET_EXIT_CODE = 3


def _session_usage(cwd: str, session_name: str) -> Optional[dict]:
    """Read the newest pi session file for *session_name* under the workdir
    and return the summed usage of its assistant messages, or None when no
    usable session exists (degradation, never crash)."""
    sessions_dir = Path.home() / ".pi" / "agent" / "sessions"
    workdir_key = "--" + cwd.replace("/", "-") + "--"
    d = sessions_dir / workdir_key
    if not d.is_dir():
        return None
    try:
        candidates = sorted(d.glob(f"*_{session_name}.jsonl"))
    except OSError:
        return None
    if not candidates:
        return None
    total = None
    try:
        with open(candidates[-1], encoding="utf-8") as f:
            for line in f:
                try:
                    msg = (json.loads(line).get("message") or {})
                except ValueError:
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                if total is None:
                    total = {}
                for k, v in usage.items():
                    if isinstance(v, (int, float)):
                        total[k] = total.get(k, 0) + v
    except OSError:
        return None
    return total


def record_event(event: str, task: str = "", detail: str = "", cwd: str = "", session_name: str = "") -> None:
    """Append one JSONL event (best-effort, never raises)."""
    if not EVENTS_FILE:
        return
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "task": task[:120],
        "detail": detail[:500],
        "usage": _session_usage(cwd, session_name) if event in ("task_finish", "task_fail") else None,
    }
    line = json.dumps(payload, ensure_ascii=False)
    try:
        p = Path(EVENTS_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        log.warning("events write failed: %s", e)
        return
    if WEBHOOK_URL:
        _post_webhook(line)


def _post_webhook(line: str) -> None:
    """Non-blocking webhook delivery with a 5s deadline; failures are
    logged only so a slow endpoint never blocks the runner."""

    def _send():
        try:
            req = urllib.request.Request(WEBHOOK_URL, data=line.encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as e:
            log.warning("webhook delivery failed: %s", e)

    threading.Thread(target=_send, daemon=True).start()


def budget_spent_today() -> int:
    """Invocations already spent today (UTC), from the daily state file."""
    if not _DAILY_STATE:
        return 0
    try:
        data = json.loads(Path(_DAILY_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if data.get("date") != datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        return 0
    return int(data.get("count", 0))


def _save_daily(count: int) -> None:
    if not _DAILY_STATE:
        return
    try:
        Path(_DAILY_STATE).parent.mkdir(parents=True, exist_ok=True)
        Path(_DAILY_STATE).write_text(json.dumps({
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "count": count,
        }), encoding="utf-8")
    except OSError:
        pass


def budget_allows() -> bool:
    """True when an invocation may start (run budget + daily budget)."""
    global INVOCATIONS
    if BUDGET_MAX and INVOCATIONS >= BUDGET_MAX:
        return False
    if DAILY_BUDGET and budget_spent_today() >= DAILY_BUDGET:
        return False
    return True


def budget_consume() -> None:
    """Count one invocation (run budget and daily budget)."""
    global INVOCATIONS
    INVOCATIONS += 1
    if DAILY_BUDGET:
        _save_daily(budget_spent_today() + 1)
