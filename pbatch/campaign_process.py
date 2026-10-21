"""Process supervision for isolated campaign pipelines."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from .campaign_budget import CampaignGuardFailed, execution_deadline
from .campaign_deadline import deadline_error, refuse_expired, supervision_failure
from .campaign_execution_guard import ExecutionGuard
from .campaign_process_group import OwnedPipeline
from .campaign_watchdog import finish_lease, lease_file, prepare_lease, wait_for_pipeline


_ACTIVE_PIPELINES: set[OwnedPipeline] = set()
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
    """Run an owned child; budget expiry is distinct from a pipeline timeout."""
    started = time.monotonic()
    deadline = execution_deadline(settings, started)
    proc = _spawn_owned(cmd, direction, worktree, pipeline_path, logfile, deadline)
    with _ACTIVE_LOCK:
        _ACTIVE_PIPELINES.add(proc)
    guard = None
    try:
        proc.capture_identity()
        guard = _execution_guard(proc, started, settings.pipeline_timeout, deadline)
        with guard if guard is not None else nullcontext():
            result = _supervise_child(proc, settings, direction, worktree, pipeline_path,
                                      logfile, started, deadline, guard)
        if deadline is not None and (deadline.expired(time.monotonic()) or guard.budget_exhausted):
            guard.stop()
            raise deadline_error(deadline, direction, worktree, pipeline_path, logfile,
                                 phase="after_supervision", child=proc,
                                 stop_requested=guard.stop_requested)
        return (-1, True, False, result[3]) if guard and guard.timed_out else result
    except BaseException as exc:
        cleanup_error = None
        try:
            guard.stop() if guard is not None else _kill_pipeline_group(proc)
        except Exception as stopped:
            cleanup_error = stopped
        failure = supervision_failure(exc, deadline, guard, proc, direction, worktree,
                                      pipeline_path, logfile, cleanup_error)
        if failure is not exc:
            raise failure from exc
        raise
    finally:
        if proc.returncode is not None:
            with _ACTIVE_LOCK:
                _ACTIVE_PIPELINES.discard(proc)


def _execution_guard(proc, started, timeout, deadline):
    if deadline is None:
        return None
    try:
        return ExecutionGuard(proc, started, timeout, deadline)
    except Exception as exc:
        raise CampaignGuardFailed("campaign deadline monitor could not initialize") from exc


def _spawn_owned(cmd, direction, worktree, pipeline_path, logfile, deadline):
    refuse_expired(deadline, direction, worktree, pipeline_path, logfile, phase="before_spawn")
    env = isolated_child_environment(worktree)
    refuse_expired(deadline, direction, worktree, pipeline_path, logfile, phase="before_spawn")
    return OwnedPipeline(subprocess.Popen(
        cmd, cwd=worktree, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True))


def _supervise_child(proc, settings, direction, worktree, pipeline_path, logfile,
                     started, deadline, guard):
    lease_path = lease_file(worktree, direction.direction_id)
    lease = prepare_lease(
        lease_path, pid=proc.pid, direction_id=direction.direction_id,
        worktree=worktree, pipeline=pipeline_path, logfile=logfile,
        timeout=settings.pipeline_timeout,
        no_progress_timeout=settings.pipeline_no_progress_timeout,
        process_start_identity=proc.start_ticks or "")
    status, reason = "interrupted", "supervision interrupted"
    try:
        if guard is not None and guard.failed:
            raise CampaignGuardFailed("campaign deadline monitor failed during lease setup")
        waited = wait_for_pipeline(
            proc, logfile=logfile, pipeline=pipeline_path,
            timeout=settings.pipeline_timeout,
            no_progress_timeout=settings.pipeline_no_progress_timeout,
            lease_path=lease_path, lease=lease, started_at=started,
            deadline=deadline, stop=guard.stop if guard is not None else proc.stop)
        if waited.budget_exhausted or (guard and guard.budget_exhausted):
            status, reason = "budget_exhausted", "campaign deadline"
            raise deadline_error(deadline, direction, worktree, pipeline_path, logfile,
                                 phase="supervision", lease=lease, child=proc,
                                 stop_requested=guard.stop_requested if guard else True)
        status, reason, result = _observed_result(proc, waited, guard)
        return result
    finally:
        if proc.returncode is not None:
            finish_lease(lease_path, lease, status, reason)
        # Otherwise retain the live/unknown owner's lease; never grant a retry.


def _observed_result(proc, waited, guard):
    if proc.returncode is None:
        raise CampaignGuardFailed("isolated child termination was not observed")
    timed_out = waited.timed_out or bool(guard and guard.timed_out)
    status = "stalled" if waited.stalled else "timed_out" if timed_out else "finished"
    reason = "no progress" if waited.stalled else "hard timeout" if timed_out else ""
    result = (-1 if timed_out else waited.returncode), timed_out, waited.stalled, waited.no_progress_seconds
    return status, reason, result


def _kill_pipeline_group(proc: OwnedPipeline) -> None:
    proc.stop()


def kill_isolated_pipelines() -> None:
    """Kill active worktree pipeline process groups during interruption."""
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE_PIPELINES)
    for proc in processes:
        _kill_pipeline_group(proc)
