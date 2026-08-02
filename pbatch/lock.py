"""Single-instance lock (R4 / T4, 7x24 governance).

A second pi-batch process must not run against the same worktree: two
round loops would double-spend quota and interleave session writes. The
lock is an O_EXCL file `.pi-batch.lock` in the process cwd holding
{pid, start, host, argv} JSON. Stale detection: a dead PID breaks the
lock automatically; a live PID older than 24h is refused with a message
naming the holder. Released in finally; skipped for --dry-run; --no-lock
is the documented escape hatch.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import log

LOCK_NAME = ".pi-batch.lock"
STALE_AGE_SEC = 24 * 3600
LOCK_EXIT_CODE = 5  # new exit-code contract (docs/RUNNING_247.md)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def acquire_lock(cwd: str = "", no_lock: bool = False) -> Path:
    """Create the lock file; raises SystemExit(5) when another live
    instance holds it. Returns the lock path (caller must release)."""
    if no_lock:
        return Path()
    lock_path = Path(cwd or os.getcwd()) / LOCK_NAME
    payload = json.dumps({
        "pid": os.getpid(),
        "start": time.time(),
        "host": os.uname().nodename,
        "argv": " ".join(__import__("sys").argv[:6]),
    })
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        _reject_or_break(lock_path)
        return acquire_lock(cwd, no_lock=False)  # stale lock broken; retry
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    return lock_path


def _reject_or_break(lock_path: Path) -> None:
    """Refuse when the holder is alive (naming it); otherwise break the
    stale lock (dead PID, or older than the 24h staleness window)."""
    try:
        holder = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        holder = {}
    pid = int(holder.get("pid", 0) or 0)
    start = float(holder.get("start", 0) or 0)
    alive = _alive(pid) if pid else False
    stale_by_age = (time.time() - start) > STALE_AGE_SEC
    if alive and not stale_by_age:
        log.error("Another pi-batch instance is running (pid=%s, host=%s, argv=%s). "
                  "Refusing to start (exit %d). Use --no-lock to override.",
                  pid, holder.get("host", "?"), holder.get("argv", "?"), LOCK_EXIT_CODE)
        raise SystemExit(LOCK_EXIT_CODE)
    log.warning("Breaking stale lock %s (pid=%s alive=%s age=%.0fs)",
                lock_path, pid, alive, time.time() - start)
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        raise SystemExit(LOCK_EXIT_CODE)


def release_lock(lock_path: Path) -> None:
    """Remove the lock file (best-effort, never raises)."""
    if not lock_path:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass
