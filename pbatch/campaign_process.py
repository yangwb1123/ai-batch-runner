"""Process supervision for isolated campaign pipelines."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

from .campaign_watchdog import finish_lease, lease_file, prepare_lease, wait_for_pipeline


_ACTIVE_PIPELINES: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def isolated_child_environment(worktree: Path) -> dict[str, str]:
    """Give each pipeline private runtime/lock state, retaining shared quotas."""
    from .user_dirs import project_runtime_dir

    env = os.environ.copy()
    env["PBATCH_DATA_DIR"] = str(project_runtime_dir(str(worktree)))
    env.pop("PBATCH_LOCK_FILE", None)
    return env


def run_isolated_child(
    cmd: list[str], settings, direction, worktree: Path,
    pipeline_path: Path, logfile: Path,
) -> tuple[int, bool, bool, float]:
    """Run one child with a lease, hard deadline, and progress watchdog."""
    proc = subprocess.Popen(
        cmd, cwd=worktree, env=isolated_child_environment(worktree),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    with _ACTIVE_LOCK:
        _ACTIVE_PIPELINES.add(proc)
    lease_path = lease_file(worktree, direction.direction_id)
    lease = {}
    timed_out = stalled = False
    no_progress_seconds = 0.0
    try:
        lease = prepare_lease(
            lease_path, pid=proc.pid, direction_id=direction.direction_id,
            worktree=worktree, pipeline=pipeline_path, logfile=logfile,
            timeout=settings.pipeline_timeout,
            no_progress_timeout=settings.pipeline_no_progress_timeout,
        )
        waited = wait_for_pipeline(
            proc, logfile=logfile, pipeline=pipeline_path,
            timeout=settings.pipeline_timeout,
            no_progress_timeout=settings.pipeline_no_progress_timeout,
            lease_path=lease_path, lease=lease,
        )
        returncode = waited.returncode
        timed_out = waited.timed_out
        stalled = waited.stalled
        no_progress_seconds = waited.no_progress_seconds
    except BaseException:
        _kill_pipeline_group(proc)
        raise
    finally:
        if lease:
            finish_lease(
                lease_path, lease,
                "stalled" if stalled else "timed_out" if timed_out else "finished",
                "no progress" if stalled else "hard timeout" if timed_out else "",
            )
        with _ACTIVE_LOCK:
            _ACTIVE_PIPELINES.discard(proc)
    return returncode, timed_out, stalled, no_progress_seconds


def _kill_pipeline_group(proc: subprocess.Popen) -> None:
    """Terminate an isolated pipeline and reap its direct child."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def kill_isolated_pipelines() -> None:
    """Kill active worktree pipeline process groups during interruption."""
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE_PIPELINES)
    for proc in processes:
        if proc.poll() is not None:
            continue
        _kill_pipeline_group(proc)
