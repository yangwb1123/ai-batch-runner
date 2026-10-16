"""Global run registry: who is running what, where, for how long.

Shared-machine lesson (LESSONS.md D3): locks only tell you a repo is
held, not WHO runs WHAT. Every pi-batch process registers itself under the
private user runtime `~/.abr/runs/<pid>.json` (one file per process — no cross-process
write contention), heartbeats its last activity every 60s via a daemon
thread, and unregisters on exit. When the home directory is read-only,
the registry deterministically falls back to a per-user runtime/temp
directory. `pi-batch ps` lists a bounded window of registered run metadata,
not all live processes across the user's repositories.

Queries select an existing registry without a write probe and use bounded,
closed metadata projections. PID presence (including conservative unknown
probe results) and a heartbeat do not verify original identity or progress.
Only an explicit --prune invocation retains the existing maintenance action.
"""

from __future__ import annotations

import json
import math
import os
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

from .user_dirs import pbatch_runtime_home
from . import process_identity
from .registry_observation import diagnostics, literal, scan

HEARTBEAT_INTERVAL = 60  # seconds

_REGISTRY_DIR = pbatch_runtime_home() / "runs"
_own_path: Path | None = None
_heartbeat_stop = threading.Event()


def _registry_dir() -> Path:
    for candidate in _registry_candidates():
        if _prepare_registry_dir(candidate):
            return candidate
    return _REGISTRY_DIR


def _registry_candidates() -> list[Path]:
    candidates = [_REGISTRY_DIR]
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        candidates.append(Path(runtime_dir) / "pi-batch" / "runs")
    user_id = os.getuid() if hasattr(os, "getuid") else os.getpid()
    candidates.append(Path(tempfile.gettempdir()) / f"pi-batch-runs-{user_id}")
    return list(dict.fromkeys(candidates))


def _prepare_registry_dir(path: Path) -> bool:
    probe = path / (f".write-probe-{os.getpid()}-"
                    f"{threading.get_ident()}-{time.time_ns()}")
    descriptor: int | None = None
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def _redacted_argv(argv: list) -> list:
    """Same redaction as the lock payload: -p/--prompt values are secrets."""
    out: list = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            out.append("<redacted>")
            continue
        if arg in ("-p", "--prompt"):
            skip_next = True
            out.append(arg)
            continue
        out.append(arg)
    return out


def register_run(mode: str, repo: str, session_name: str = "",
                 status: str = "queued", extra: dict | None = None) -> Path:
    """Publish this process in the global registry; returns the file path.
    Call mark_running() once the lock is held, heartbeat() keeps the
    process visible, unregister() removes it on exit."""
    global _own_path
    meta = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "start": time.time(),
        "mode": mode,
        "repo": repo,
        "session": session_name,
        "argv": _redacted_argv(sys.argv),
        "status": status,
        "last_active": time.time(),
    }
    if extra:
        meta.update(extra)
    path = _registry_dir() / f"{os.getpid()}.json"
    try:
        _write_atomic(path, meta)
    except OSError:
        _own_path = None
        return path
    _own_path = path
    _heartbeat_stop.clear()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    return _own_path


def mark_running() -> None:
    """Transition this run from queued/starting to running (lock held)."""
    _patch_own({"status": "running"})


def mark_waiting_lock() -> None:
    """The process is queued behind a held lock (visible as queued)."""
    _patch_own({"status": "queued"})


def heartbeat() -> None:
    _patch_own({"last_active": time.time()})


def unregister() -> None:
    """Remove this process from the registry (exit path)."""
    global _own_path
    _heartbeat_stop.set()
    if _own_path is not None:
        try:
            _own_path.unlink(missing_ok=True)
        except OSError:
        # best-effort I/O：失败不阻塞主流程（已验证有意）
            pass
        _own_path = None


def _patch_own(meta: dict) -> None:
    if _own_path is None:
        return
    try:
        data = json.loads(_own_path.read_text(encoding="utf-8"))
        data.update(meta)
        _write_atomic(_own_path, data)
    except (OSError, ValueError):
        pass  # registry is best-effort; never fail the run


def _write_atomic(path: Path, meta: dict) -> None:
    tmp = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _heartbeat_loop() -> None:
    while not _heartbeat_stop.wait(HEARTBEAT_INTERVAL):
        heartbeat()


def _read_registry_dir() -> Path:
    """Choose only existing roots; an unsafe existing root is not a fallback grant."""
    candidates = _registry_candidates()
    if _own_path is not None:
        candidates = [_own_path.parent, *candidates]
    for path in dict.fromkeys(candidates):
        try:
            mode = path.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return path
        if stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            return path
    return candidates[0] if candidates else _REGISTRY_DIR


def _observed_time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 10**15 else None


def _run_record(path: Path, meta: dict) -> dict | None:
    pid = meta.get("pid")
    if not process_identity.valid_pid(pid) or path.stem != str(pid):
        return None
    host, status = meta.get("host"), literal(meta.get("status"), 64)
    presence = "unknown_host"
    if isinstance(host, str) and host == socket.gethostname():
        presence = "pid_present_or_unknown" if _alive(pid) else "pid_absent"
        reported = "dead" if presence == "pid_absent" else status
    else:
        reported = "unknown"
    return {"pid": pid, "host": literal(host, 128), "status": reported,
            "registered_status": status, "presence": presence,
            "start": _observed_time(meta.get("start")),
            "last_active": _observed_time(meta.get("last_active")),
            "mode": literal(meta.get("mode"), 64), "repo": literal(meta.get("repo"), 1024),
            "session": literal(meta.get("session"), 128), "_file": str(path),
            "liveness": "not_verified", "progress": "untracked", "controllable": False}


def run_snapshot(*, include_files: bool = False) -> dict:
    """One selected existing registry window, not an all-process inventory."""
    pairs, coverage = scan(_read_registry_dir())
    runs = []
    for path, value in pairs:
        record = _run_record(path, value)
        if record is None:
            coverage["malformed"] += 1
        else:
            if not include_files:
                record.pop("_file")
            runs.append(record)
    return {"schema_version": 1, "runs": runs, "coverage": coverage,
            "diagnostics": diagnostics(coverage), "complete_inventory": False,
            "scope": "bounded_selected_existing_registry_metadata_not_process_inventory",
            "grants_execution": False}


def list_runs() -> list[dict]:
    """Legacy list projection; CLI reports coverage and never exposes private _file."""
    return run_snapshot(include_files=True)["runs"]


def prune_dead() -> int:
    """Remove registry files whose pid is dead; returns count removed."""
    removed = 0
    for run in list_runs():
        if (run["status"] == "dead" and run.get("host") == socket.gethostname()
                and not _alive(run.get("pid"))):
            try:
                Path(run["_file"]).unlink(missing_ok=True)
                removed += 1
            except OSError:
            # best-effort I/O：失败不阻塞主流程（已验证有意）
                pass
    return removed


def _alive(pid: int) -> bool:
    return process_identity.pid_alive(pid)


def _timestamp(value: object, fallback: float) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return timestamp if math.isfinite(timestamp) else fallback


def format_ps(runs: list[dict]) -> str:
    """Literal metadata table; never render arbitrary argv or private extra fields."""
    lines = [f"{'PID':<8} {'MODE':<10} {'REPO':<44} {'SESSION':<24} "
             f"{'AGE':<7} {'IDLE':<6} STATE    PRESENCE"]
    now = time.time()
    for record in runs:
        start, heartbeat = _observed_time(record.get("start")), _observed_time(record.get("last_active"))
        age = "未追踪" if start is None or start > now else _fmt_age(now - start)
        idle = "未追踪" if heartbeat is None or heartbeat > now else _fmt_age(now - heartbeat)
        repo = literal(record.get("repo"), 1024)
        if len(repo) > 44:
            repo = "..." + repo[-41:]
        lines.append(f"{record.get('pid', 0):<8} {literal(record.get('mode'), 10):<10} "
                     f"{repo:<44} {literal(record.get('session'), 24):<24} "
                     f"{age:<7} {idle:<6} {literal(record.get('status'), 64):<8} "
                     f"{literal(record.get('presence'), 64)}")
    return "\n".join(lines)


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def ps_main(argv: list) -> None:
    """`pi-batch ps [--json] [--prune]`: bounded registered metadata, not live inventory."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="pi-batch.py ps",
        description="Observe bounded registered pi-batch metadata, not a live process inventory.")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--prune", action="store_true",
                        help="remove dead-pid registry entries first")
    args = parser.parse_args(argv)
    if args.prune:
        prune_dead()
    view = run_snapshot()
    runs = view["runs"]
    for note in view["diagnostics"]:
        print(f"ps: {note}", file=sys.stderr)
    if args.json:
        public = [{key: value for key, value in row.items() if key != "_file"} for row in runs]
        print(json.dumps(public, ensure_ascii=False, indent=2, allow_nan=False))
        return
    if not runs:
        print("No registrations in this bounded window; not proof that no processes are running.")
    else:
        print(format_ps(runs))
    print("只读有界注册元数据；PID/心跳不是原进程身份或有效进展；无控制权。")
