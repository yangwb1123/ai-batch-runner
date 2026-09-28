"""Bounded process leases and progress supervision for campaign pipelines."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .campaign_models import safe_slug
from .config import data_dir
from . import process_identity

_MAX_LEASE_BYTES = 16 * 1024
_LEASE_WRITE_INTERVAL = 15.0
_POLL_SECONDS = 1.0
_STOP_GRACE_SECONDS = 15.0
_MAX_PROGRESS_DIRS = 64
_MAX_PROGRESS_FILES = 512


@dataclass(frozen=True)
class PipelineWait:
    returncode: int
    timed_out: bool = False
    stalled: bool = False
    no_progress_seconds: float = 0.0


def lease_file(worktree: Path, direction_id: str) -> Path:
    """Return the bounded, worktree-local lease path for one direction."""
    return data_dir(str(worktree)) / "campaign" / "leases" / (
        safe_slug(direction_id, fallback="pipeline") + ".json")


def process_start_ticks(pid: int) -> str:
    """Read Linux process start ticks to avoid trusting a reused PID."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(
            encoding="utf-8", errors="replace")
        tail = raw.rsplit(")", 1)[-1].split()
        # After the command name, field 3 is state; field 22 is starttime.
        return tail[19] if len(tail) > 19 else ""
    except (OSError, ValueError):
        return ""


def _bounded_text(value: object, limit: int = 512) -> str:
    return str(value or "")[:limit]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _encode(payload: dict) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False,
                         sort_keys=True).encode("utf-8")
    if len(encoded) > _MAX_LEASE_BYTES:
        raise ValueError("pipeline lease exceeds bounded size")
    return encoded + b"\n"


def _write_json(path: Path, payload: dict) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symlink pipeline lease: {path}")
    encoded = _encode(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _create_json(path: Path, payload: dict) -> None:
    """Create the owner record atomically; replace is not safe for acquisition."""
    if path.is_symlink():
        raise ValueError(f"refusing symlink pipeline lease: {path}")
    encoded = _encode(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        if path.stat().st_size > _MAX_LEASE_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _pid_is_same_process(payload: dict) -> bool:
    pid = payload.get("pid")
    if not process_identity.pid_alive(pid):
        return False
    expected = _bounded_text(payload.get("process_start_ticks"))
    actual = process_start_ticks(pid)
    if expected and actual:
        return expected == actual
    return True


def prepare_lease(path: Path, *, pid: int, direction_id: str,
                  worktree: Path, pipeline: Path, logfile: Path,
                  timeout: int, no_progress_timeout: int) -> dict:
    """Create a lease, rejecting a still-live owner and removing stale data."""
    exists = path.exists() or path.is_symlink()
    previous = _read_json(path)
    if exists and not previous:
        raise RuntimeError("pipeline lease exists but is unreadable")
    if previous and _pid_is_same_process(previous):
        raise RuntimeError(
            f"pipeline lease is already held by pid {previous.get('pid')}"
        )
    if exists:
        try:
            path.unlink()
        except OSError as exc:
            raise RuntimeError(f"cannot clear stale pipeline lease: {exc}") from exc
    payload = {
        "schema": 1,
        "status": "running",
        "pid": int(pid),
        "process_start_ticks": process_start_ticks(pid),
        "direction_id": _bounded_text(direction_id),
        "worktree": _bounded_text(worktree.resolve()),
        "pipeline": _bounded_text(pipeline.resolve()),
        "log_file": _bounded_text(logfile.resolve()),
        "started_at": _now(),
        "timeout_seconds": max(0, int(timeout)),
        "no_progress_timeout_seconds": max(0, int(no_progress_timeout)),
        "last_progress_at": _now(),
    }
    try:
        _create_json(path, payload)
    except FileExistsError as exc:
        current = _read_json(path)
        if current and _pid_is_same_process(current):
            raise RuntimeError(
                f"pipeline lease is already held by pid {current.get('pid')}"
            ) from exc
        raise RuntimeError("pipeline lease was concurrently acquired") from exc
    return payload


def _touch_lease(path: Path, payload: dict, *, progress_at: str | None = None) -> None:
    payload = dict(payload)
    payload["heartbeat_at"] = _now()
    if progress_at is not None:
        payload["last_progress_at"] = progress_at
    _write_json(path, payload)


def finish_lease(path: Path, payload: dict, status: str, reason: str = "") -> None:
    """Write a terminal lease snapshot and remove it without masking outcome."""
    try:
        payload = dict(payload)
        payload.update({"status": _bounded_text(status, 64),
                        "reason": _bounded_text(reason), "finished_at": _now()})
        _write_json(path, payload)
        path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _marker(paths: tuple[Path, ...]) -> tuple[tuple[str, int, int], ...]:
    result = []
    for path in paths:
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            continue
        result.append((str(path), int(info.st_mtime_ns), int(info.st_size)))
    return tuple(result)


def _artifact_marker(pipeline: Path) -> tuple[tuple[str, int, int], ...]:
    """Fingerprint a bounded prefix of deliverables, never heartbeat logs."""
    root = pipeline.parent / "artifacts"
    if root.is_symlink() or not root.is_dir():
        return ()
    pending = [root]
    result = []
    visited = 0
    while pending and visited < _MAX_PROGRESS_DIRS and len(result) < _MAX_PROGRESS_FILES:
        current = pending.pop()
        visited += 1
        try:
            with os.scandir(current) as scan:
                entries = sorted(scan, key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                    result.append((entry.path, int(info.st_mtime_ns), int(info.st_size)))
                    if len(result) >= _MAX_PROGRESS_FILES:
                        break
            except OSError:
                continue
    return tuple(sorted(result))


def _stop_group(proc: subprocess.Popen, grace: float = _STOP_GRACE_SECONDS) -> None:
    """Ask the whole child group to stop, then reap/kill it deterministically."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + max(0.0, grace)
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(min(_POLL_SECONDS, max(0.01, deadline - time.monotonic())))
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=2)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def wait_for_pipeline(proc: subprocess.Popen, *, logfile: Path, pipeline: Path,
                      timeout: int, no_progress_timeout: int,
                      lease_path: Path, lease: dict) -> PipelineWait:
    """Monitor deadlines and bounded deliverable artifacts.

    Logs, lease writes, and the static pipeline manifest are not progress.
    """
    started = time.monotonic()
    last_progress = started
    previous_marker = _artifact_marker(pipeline)
    last_lease_write = started
    while True:
        returncode = proc.poll()
        if returncode is not None:
            return PipelineWait(returncode)
        now = time.monotonic()
        current_marker = _artifact_marker(pipeline)
        progressed = current_marker != previous_marker
        if progressed:
            previous_marker = current_marker
            last_progress = now
        if now - last_lease_write >= _LEASE_WRITE_INTERVAL:
            _touch_lease(lease_path, lease,
                         progress_at=_now() if progressed else None)
            last_lease_write = now
        if timeout > 0 and now - started >= timeout:
            _stop_group(proc, grace=0)
            return PipelineWait(
                -1, timed_out=True,
                no_progress_seconds=(max(0.0, now - last_progress)
                                     if no_progress_timeout > 0 else 0.0))
        if (no_progress_timeout > 0 and
                now - last_progress >= no_progress_timeout):
            stalled_for = max(0.0, now - last_progress)
            _stop_group(proc, grace=0)
            return PipelineWait(-1, stalled=True,
                                no_progress_seconds=stalled_for)
        time.sleep(_POLL_SECONDS)
