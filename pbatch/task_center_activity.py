"""Read the existing process registry without its write-probe/prune path.

PID presence and heartbeat are activity observations, not progress or ownership
proof. This projection deliberately issues no process-control capability.
"""
from __future__ import annotations

import os
import socket
import tempfile
import time
from pathlib import Path

from . import process_identity
from .task_center_io import display
from .registry_observation import diagnostics, literal, scan
from .task_center_metrics import timestamp
from .task_sources import observation_roots

MAX_PROCESSES = 64


def activity_snapshot() -> dict:
    roots = [home / "runs" for home in observation_roots()]
    if os.environ.get("XDG_RUNTIME_DIR"):
        roots.append(Path(os.environ["XDG_RUNTIME_DIR"]) / "pi-batch" / "runs")
    uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
    roots.append(Path(tempfile.gettempdir()) / f"pi-batch-runs-{uid}")
    rows, errors, seen, coverage = [], [], set(), []
    for root in dict.fromkeys(roots):
        coverage.append(_read_directory(root, rows, errors, seen))
    return {"processes": rows, "diagnostics": errors, "coverage": coverage,
            "complete_inventory": False, "grants_execution": False,
            "scope": "bounded_known_registry_windows_not_process_or_goal_inventory",
            "note": "PID presence and heartbeat do not prove task progress or original process identity"}


def _read_directory(root: Path, rows: list, errors: list, seen: set) -> dict:
    pairs, info = scan(root, maximum=MAX_PROCESSES - len(rows))
    admitted = 0
    for path, value in pairs:
        key = str(path)
        if key in seen:
            continue
        row = _row(path, value)
        if row is None:
            info["malformed"] += 1
        else:
            seen.add(key)
            rows.append(row)
            admitted += 1
    info["admitted_processes"] = admitted if info["available"] else None
    errors.extend(diagnostics(info))
    return info


def _row(path: Path, value: dict | None) -> dict | None:
    if value is None or not process_identity.valid_pid(value.get("pid")):
        return None
    pid = value["pid"]
    if path.stem != str(pid):
        return None
    recorded_host = value.get("host")
    host = _label(recorded_host, 128)
    presence = "unknown_host"
    if isinstance(recorded_host, str) and recorded_host == socket.gethostname():
        presence = "pid_present" if process_identity.pid_alive(pid) else "pid_absent"
    start, heartbeat = timestamp(value.get("start")), timestamp(value.get("last_active"))
    age, clock = _heartbeat(heartbeat)
    return {"pid": pid, "host": host, "workspace": _label(value.get("repo"), 1024),
            "mode": _label(value.get("mode"), 64), "session": _label(value.get("session"), 128),
            "registered_status": _label(value.get("status"), 64), "presence": presence,
            "presence_basis": "conservative_pid_boolean_including_unknown_not_start_identity",
            "registered_start": start, "heartbeat_age_seconds": age, "heartbeat_clock": clock,
            "liveness": "not_verified", "progress": "untracked", "controllable": False}


def _label(value: object, maximum: int) -> str:
    return literal(display(value, maximum), maximum)


def _heartbeat(stamp: float | None) -> tuple[float | None, str]:
    if stamp is None:
        return None, "untracked"
    age = time.time() - stamp
    return (None, "reported_future") if age < 0 else (age, "reader_clock_difference_not_verified")
