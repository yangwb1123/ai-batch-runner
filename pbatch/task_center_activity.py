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
from .task_center_io import checked_path, display, read_object
from .task_center_metrics import timestamp
from .task_sources import observation_roots

MAX_PROCESSES = 64


def activity_snapshot() -> dict:
    roots = [home / "runs" for home in observation_roots()]
    if os.environ.get("XDG_RUNTIME_DIR"):
        roots.append(Path(os.environ["XDG_RUNTIME_DIR"]) / "pi-batch" / "runs")
    uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
    roots.append(Path(tempfile.gettempdir()) / f"pi-batch-runs-{uid}")
    rows, errors, seen = [], [], set()
    for root in dict.fromkeys(roots):
        try:
            _read_directory(root, rows, errors, seen)
        except (OSError, ValueError) as exc:
            errors.append(display(str(exc)))
    return {"processes": rows, "diagnostics": errors,
            "note": "PID presence and heartbeat do not prove task progress or original process identity"}


def _read_directory(root: Path, rows: list, errors: list, seen: set) -> None:
    root = checked_path(root)
    if not root.is_dir():
        return
    for index, path in enumerate(root.iterdir()):
        if len(rows) >= MAX_PROCESSES or index >= MAX_PROCESSES * 4:
            errors.append("process registry scan limited")
            break
        if path.suffix != ".json":
            continue
        try:
            value = read_object(path, 16 * 1024)
            row = _row(path, value)
            if row and (row["host"], row["pid"], row["registered_start"]) not in seen:
                seen.add((row["host"], row["pid"], row["registered_start"]))
                rows.append(row)
        except (OSError, ValueError) as exc:
            errors.append(display(str(exc)))


def _row(path: Path, value: dict | None) -> dict | None:
    if value is None or not process_identity.valid_pid(value.get("pid")):
        return None
    pid = value["pid"]
    if path.stem != str(pid):
        return None
    host = display(value.get("host"), 128)
    presence = "unknown_host"
    if host == socket.gethostname():
        presence = "pid_present" if process_identity.pid_alive(pid) else "pid_absent"
    start, heartbeat = timestamp(value.get("start")), timestamp(value.get("last_active"))
    return {"pid": pid, "host": host, "workspace": display(value.get("repo"), 1024),
            "mode": display(value.get("mode"), 64), "session": display(value.get("session"), 128),
            "registered_status": display(value.get("status"), 64), "presence": presence,
            "registered_start": start,
            "heartbeat_age_seconds": max(0, time.time() - heartbeat) if heartbeat is not None else None,
            "progress": "untracked", "controllable": False}
