"""Shared, bounded run ledger for heterogeneous CLI agents."""

from __future__ import annotations

import argparse
from . import file_lock as fcntl
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import config
from .config import log


LEDGER_MAX_BYTES = 16 * 1024 * 1024
LEDGER_MAX_LINE_BYTES = 4 * 1024
LEDGER_READ_LINE_MAX_BYTES = 4 * 1024
LEDGER_KEEP_LINES = 1000
QUERY_MAX_RECORDS = 1000
OUTPUT_PATH_MAX_CHARS = 512
DEFAULT_AGENTS = ("pi", "codex", "claude", "gemini", "opencode")


@dataclass(frozen=True)
class LedgerQuerySnapshot:
    """Bounded read result with diagnostics hidden by the legacy list API."""

    records: tuple[dict, ...] = ()
    skipped_records: int = 0
    truncated: bool = False
    error: str = ""
    path: str = ""
    bytes_read: int = 0


def ledger_path(data_dir) -> Path:
    """Return the shared ledger path below a configured runtime data root."""
    return Path(data_dir) / "agents" / "ledger.jsonl"


def _timestamp(ts):
    if ts is None:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return ts
    return str(ts)


def _brief(value: object, maximum: int) -> str:
    return str(value or "")[:maximum]


def _output_path(value: object) -> str:
    original = str(value or "")
    if not original:
        return ""
    try:
        resolved = Path(original).expanduser().resolve()
        try:
            shown = str(resolved.relative_to(Path.cwd().resolve()))
        except ValueError:
            shown = original
    except (OSError, RuntimeError, ValueError):
        shown = original
    return _brief(shown, OUTPUT_PATH_MAX_CHARS)


def _usage(value: object) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    result = {}
    priority = ("cost_total", "total_cost", "cost", "totalTokens",
                "total_tokens", "inputTokens", "outputTokens",
                "input_tokens", "output_tokens")
    keys = [key for key in priority if key in value]
    keys.extend(key for key in value if key not in priority)
    for key in keys[:8]:
        item = value[key]
        if not isinstance(key, str) or isinstance(item, bool):
            continue
        if not isinstance(item, (int, float)):
            continue
        try:
            number = float(item)
            if not math.isfinite(number) or abs(number) > 1e15:
                continue
        except (OverflowError, ValueError):
            continue
        result[_brief(key, 32)] = item
    return result or None


def _route_fields(source: str, tier: str, requested_tier: str,
                 downgraded, profile_mode) -> dict:
    return {
        "route_source": _brief(source, 64),
        "route_tier": _brief(tier, 32),
        "route_requested_tier": _brief(requested_tier, 32),
        "route_downgraded": downgraded if isinstance(downgraded, bool) else None,
        "route_profile_mode": _profile_mode(profile_mode),
    }


def _phase_value(value: object) -> str:
    return (value if isinstance(value, str)
            and value in {"execution", "validation"} else "execution")


def _entry(*, agent: str, provider: str, run_id: str, session_id: str,
           session_name: str, status: str, task_brief: str,
           outputs: Iterable[object], elapsed_ms: int, exit_code: int,
           attempt: int = 0, model: str = "", stage: str = "",
           role: str = "", task_id: str = "", phase: str = "execution",
           task_fingerprint: str = "", route_source: str = "",
           route_tier: str = "", route_requested_tier: str = "",
           route_downgraded=None,
           route_profile_mode=None, usage: Optional[dict] = None,
           ts=None) -> dict:
    return {
        "ts": _timestamp(ts),
        "agent": str(agent or ""),
        "provider": str(provider or ""),
        "run_id": str(run_id or ""),
        "session_id": str(session_id or ""),
        "session_name": str(session_name or ""),
        "status": str(status or ""),
        "task_brief": _brief(" ".join(str(task_brief or "").split()), 120),
        "outputs": [path for path in
                    (_output_path(item) for item in islice(outputs or (), 8)) if path],
        "elapsed_ms": max(0, int(elapsed_ms or 0)),
        "exit_code": int(exit_code or 0),
        "attempt": max(0, int(attempt or 0)),
        "model": _brief(model, 256),
        "stage": _brief(stage, 128),
        "role": _brief(role, 128),
        "task_id": _brief(task_id, 128),
        "task_fingerprint": _brief(task_fingerprint, 64),
        **_route_fields(route_source, route_tier, route_requested_tier,
                        route_downgraded, route_profile_mode),
        "phase": _phase_value(phase),
        "usage": _usage(usage),
    }


def _tail_bytes(path: Path, maximum: int) -> bytes:
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - max(1, maximum))
        handle.seek(start)
        data = handle.read(max(1, maximum))
    if start:
        _, separator, data = data.partition(b"\n")
        return data if separator else b""
    return data


def _tail_lines(path: Path, maximum: int) -> list[bytes]:
    if not path.is_file():
        return []
    return [line for line in _tail_bytes(path, maximum).splitlines() if line]


def _fit_latest(lines: list[bytes], incoming: int) -> bytes:
    budget = max(0, LEDGER_MAX_BYTES - incoming)
    kept: list[bytes] = []
    used = 0
    for line in reversed(lines[-LEDGER_KEEP_LINES:]):
        size = len(line) + 1
        if used + size > budget:
            break
        kept.append(line)
        used += size
    return b"\n".join(reversed(kept)) + (b"\n" if kept else b"")


def _rotate(path: Path, incoming: int) -> None:
    current = path.stat().st_size if path.exists() else 0
    if current + incoming <= LEDGER_MAX_BYTES:
        return
    payload = _fit_latest(_tail_lines(path, LEDGER_MAX_BYTES), incoming)
    fd, temporary = tempfile.mkstemp(prefix="ledger.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def record(data_dir, *, agent, provider="", run_id="", session_id="",
           session_name="", status, task_brief, outputs=(), elapsed_ms=0,
           exit_code=0, attempt=0, model="", stage="", role="", task_id="",
           phase="execution", task_fingerprint="", route_source="",
           route_tier="", route_requested_tier="", route_downgraded=None,
           route_profile_mode=None,
           usage=None, ts=None) -> bool:
    """Append one atomic JSON line; operational failures only warn."""
    try:
        entry = _entry(
            agent=agent, provider=provider, run_id=run_id,
            session_id=session_id, session_name=session_name, status=status,
            task_brief=task_brief, outputs=outputs, elapsed_ms=elapsed_ms,
            exit_code=exit_code, attempt=attempt, model=model, stage=stage,
            role=role, task_id=task_id, phase=phase,
            task_fingerprint=task_fingerprint, route_source=route_source,
            route_tier=route_tier, route_requested_tier=route_requested_tier,
            route_downgraded=route_downgraded,
            route_profile_mode=route_profile_mode, usage=usage, ts=ts)
        encoded = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) +
                   "\n").encode("utf-8")
        capacity = min(LEDGER_MAX_LINE_BYTES, LEDGER_MAX_BYTES)
        if len(encoded) > capacity:
            log.warning("AGENTS ledger record exceeds %d bytes; ignored", capacity)
            return False
        path = ledger_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _rotate(path, len(encoded))
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                if os.write(fd, encoded) != len(encoded):
                    raise OSError("short append to agent ledger")
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)
        return True
    except Exception as exc:
        log.warning("AGENTS ledger write failed: %s", exc)
        return False


def _ts_key(value: object) -> tuple[int, object]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, float(value))
    text = str(value or "")
    try:
        return (0, float(text))
    except ValueError:
        pass
    try:
        return (0, datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return (1, text)


def query(data_dir, *, agent="", limit=50, since_ts=None) -> list[dict]:
    """Return the most recent matching records, ordered by timestamp."""
    return list(query_snapshot(
        data_dir, agent=agent, limit=limit, since_ts=since_ts).records)


def query_snapshot(data_dir, *, agent="", limit=50,
                   since_ts=None,
                   record_filter: Optional[Callable[[dict], bool]] = None
                   ) -> LedgerQuerySnapshot:
    """Read recent records with corruption and truncation diagnostics.

    ``record_filter`` is applied before the result limit so targeted reports
    do not lose matching older rows behind unrelated recent activity.
    """
    count = _query_limit(limit)
    path = ledger_path(data_dir)
    if not count:
        return LedgerQuerySnapshot(path=str(path))
    try:
        lines = _tail_lines(path, LEDGER_MAX_BYTES)
        size = path.stat().st_size if path.is_file() else 0
    except OSError as exc:
        return LedgerQuerySnapshot(
            error=_brief(str(exc), 500), path=str(path))
    records, skipped = _decode_query_lines(lines, agent, since_ts,
                                            record_filter)
    records.sort(key=lambda item: _ts_key(item.get("ts")))
    truncated = len(records) > count
    return LedgerQuerySnapshot(
        tuple(records[-count:]), skipped, truncated, "", str(path),
        min(size, LEDGER_MAX_BYTES))


def _query_limit(limit: object) -> int:
    try:
        return max(0, min(int(limit), QUERY_MAX_RECORDS))
    except (TypeError, ValueError):
        return 50


def _decode_query_lines(lines: list[bytes], agent: str, since_ts: object,
                        record_filter: Optional[Callable[[dict], bool]] = None
                        ) -> tuple[list[dict], int]:
    records, skipped = [], 0
    since_key = _ts_key(since_ts) if since_ts is not None else None
    for raw in lines:
        if len(raw) > LEDGER_READ_LINE_MAX_BYTES:
            skipped += 1
            continue
        try:
            item = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            skipped += 1
            continue
        if not isinstance(item, dict):
            skipped += 1
            continue
        if agent and item.get("agent") != agent:
            continue
        if since_key is not None and _ts_key(item.get("ts")) < since_key:
            continue
        if record_filter is not None and not record_filter(item):
            continue
        records.append(item)
    return records, skipped


def _configured_agent(name: str) -> bool:
    section = config._agent_section(name)
    if section:
        return True
    return name == "pi" and bool(getattr(config, "_AGENT_CFG", {}))


def agents_health(data_dir, bin_names) -> list[dict]:
    """Report executable, adapter, and latest observed status per agent."""
    rows = []
    for name in bin_names:
        latest = query(data_dir, agent=name, limit=1)
        rows.append({
            "agent": str(name),
            "available": shutil.which(str(name)) is not None,
            "configured": _configured_agent(str(name)),
            "last_status": latest[-1].get("status", "") if latest else "",
        })
    return rows


def _result_status(result) -> str:
    reason = str(getattr(result, "reason", "") or "").lower()
    if getattr(result, "success", False):
        return "succeeded"
    if "cancel" in reason:
        return "cancelled"
    if getattr(result, "validation_ok", None) is False or "validation fail" in reason:
        return "rejected"
    return "failed"


def _profile_mode(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, min(10, value))


def _task_fingerprint(task, workdir: str, stage: str, role: str) -> str:
    """Hash task semantics for paired evaluation without storing its prompt."""
    try:
        workspace = str(Path(workdir).resolve())
    except (OSError, RuntimeError, ValueError):
        workspace = str(workdir)
    feedback_marker = "\n\n[validator feedback from previous attempt]\n"
    prompt = str(task.prompt).split(feedback_marker, 1)[0]
    fields = (workspace, prompt, stage, role, str(task.validate or ""),
              str(task.output or ""), task.effective_agent(),
              str(task.thinking or ""), str(config.AGENT_THINKING_OVERRIDE or ""),
              str(task.tools or ""), str(task.exclude_tools or ""),
              str(task.no_tools), str(task.timeout))
    digest = hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()
    return digest[:32]


def record_result(result, task, workdir: str, *, phase: str = "execution") -> bool:
    """Bridge a runner result into the stable ledger record contract."""
    output = task.output_path()
    stage = str(task.memory.get("stage", ""))
    role = str(task.memory.get("role", ""))
    return record(
        config.data_dir(workdir), agent=task.effective_agent(),
        provider=task.provider, run_id=result.run_id,
        session_id=result.session_id, session_name=result.session_name,
        status=_result_status(result), task_brief=task.prompt,
        outputs=([str(output)] if output else ()),
        elapsed_ms=round(result.elapsed * 1000), exit_code=result.returncode,
        attempt=int(getattr(result, "attempt", 0) or 0), model=task.model,
        stage=stage, role=role,
        task_id=str(getattr(result, "task_id", "") or ""), phase=phase,
        task_fingerprint=_task_fingerprint(task, workdir, stage, role),
        route_source=str(task.memory.get("model_route_source", "")),
        route_tier=str(task.memory.get("model_route_tier", "")),
        route_requested_tier=str(
            task.memory.get("model_route_requested_tier", "")),
        route_downgraded=task.memory.get("model_route_downgraded"),
        route_profile_mode=task.memory.get("model_route_profile_mode"),
        usage=getattr(result, "usage", None))


def _positive_limit(value: str) -> int:
    number = int(value)
    if number <= 0 or number > QUERY_MAX_RECORDS:
        raise argparse.ArgumentTypeError(
            f"limit must be between 1 and {QUERY_MAX_RECORDS}")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-batch agents")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="show configured agent adapters")
    listing.add_argument("--json", action="store_true")
    sessions = commands.add_parser("sessions", help="show shared run sessions")
    sessions.add_argument("--agent", default="")
    sessions.add_argument("--limit", type=_positive_limit, default=50)
    sessions.add_argument("--json", action="store_true")
    return parser


def _session_rows(entries: list[dict]) -> list[dict]:
    return [{
        "time": str(item.get("ts", "")),
        "agent": str(item.get("agent", "")),
        "status": str(item.get("status", "")),
        "session": str(item.get("session_name") or item.get("session_id") or ""),
        "task": _brief(" ".join(str(item.get("task_brief", "")).split()), 40),
    } for item in entries]


def _print_health(rows: list[dict]) -> None:
    print(f"{'AGENT':<12} {'EXECUTABLE':<10} {'ADAPTER':<8} LAST STATUS")
    for row in rows:
        print(f"{row['agent']:<12} {('yes' if row['available'] else 'no'):<10} "
              f"{('yes' if row['configured'] else 'no'):<8} "
              f"{row['last_status'] or '-'}")


def _print_sessions(rows: list[dict]) -> None:
    if not rows:
        print("no runs")
        return
    print(f"{'TIME':<21} {'AGENT':<12} {'STATUS':<10} {'SESSION':<18} TASK")
    for row in rows:
        print(f"{row['time']:<21} {row['agent']:<12} {row['status']:<10} "
              f"{row['session'][:18]:<18} {row['task']}")


def agents_main(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``pi-batch agents list|sessions``."""
    args = _parser().parse_args(argv)
    root = config.data_dir(os.getcwd())
    if args.command == "list":
        rows = agents_health(root, DEFAULT_AGENTS)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            _print_health(rows)
        return 0
    rows = _session_rows(query(root, agent=args.agent, limit=args.limit))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        _print_sessions(rows)
    return 0
