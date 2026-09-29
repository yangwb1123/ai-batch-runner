"""Detached supervisor and executor for explicitly persistent TUI pipelines."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Optional

from . import config
from .lock import acquire_lock, release_lock
from .pipeline import load_pipeline, run_pipeline
from .tui_native import NativePipelineControlChannel
from .tui_pipeline import (
    _handoff_text, _pipeline_cwd, _pipeline_handoff_data,
    _pipeline_runtime_config, _pipeline_session_name, _prepare_pipeline,
)
from .tui_pipeline_jobs import (
    MAX_RUN_ATTEMPTS, PipelineJobStoreError, TuiPipelineJobStore,
    pipeline_fingerprint,
)


@dataclass
class _WorkerOwner:
    options: Any

    def _option_value(self, name: str) -> str:
        return str(getattr(self.options, name, "") or "")

    def _emit_from_backend(self, _kind: str, _text: str) -> None:
        return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        action = _execute if args.execute else _supervise
        return action(args.workspace, args.job_id, args.owner_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        config.log.error("persistent pipeline worker failed: %s", exc)
        return 1


def _supervise(workspace_value: str, job_id: str, owner_id: str) -> int:
    root = Path(workspace_value).expanduser().resolve(strict=True)
    store = TuiPipelineJobStore(root, owner_id=owner_id)
    store.adopt_supervisor(job_id)
    child = None
    while True:
        job = store.read()
        if not job or job["job_id"] != job_id:
            return 1
        state = job["state"]
        if state in {"completed", "failed", "cancelled"}:
            return 0
        if state == "handoff_pending":
            _release_supervised_handoff(store, job)
            return 0
        if state == "queued":
            finished, child = _service_queued_job(
                store, root, job, owner_id)
            if finished:
                return 0
            continue
        if state != "running":
            return 1
        finished, child = _service_running_job(
            store, root, job, owner_id, child)
        if finished:
            return 0


def _service_queued_job(store: TuiPipelineJobStore, root: Path,
                        job: dict, owner_id: str) -> tuple[bool, Any]:
    job_id = job["job_id"]
    if job["cancel_requested"]:
        store.finish_queued_cancel(job_id)
        return True, None
    if job["attempts"] >= MAX_RUN_ATTEMPTS:
        store.fail_exhausted(job_id, "worker recovery attempt budget exhausted")
        return True, None
    delay = job["retry_after"] - int(time.time())
    if delay > 0:
        time.sleep(min(0.25, delay))
        return False, None
    store.claim_run(job_id)
    store.adopt_supervisor(job_id)
    child = _spawn_executor_or_defer(store, root, job_id, owner_id)
    return False, child


def _service_running_job(store: TuiPipelineJobStore, root: Path,
                         job: dict, owner_id: str, child: Any
                         ) -> tuple[bool, Any]:
    if child is not None and child.poll() is None:
        time.sleep(0.2)
        return False, child
    if child is not None:
        job = store.read()
        if not job or job["state"] in {"completed", "failed", "cancelled"}:
            return True, None
        if job["state"] == "handoff_pending":
            _release_supervised_handoff(store, job)
            return True, None
        if job["state"] == "queued":
            return False, None
        if job["state"] != "running":
            return True, None
    if job["cancel_requested"]:
        store.finish_supervised_cancel(job["job_id"])
        return True, None
    if child is not None:
        if job["attempts"] >= MAX_RUN_ATTEMPTS:
            store.fail_exhausted(
                job["job_id"], "worker recovery attempt budget exhausted")
            return True, None
        store.claim_run(job["job_id"])
        store.adopt_supervisor(job["job_id"])
    new_child = _spawn_executor_or_defer(store, root, job["job_id"], owner_id)
    return False, new_child


def _spawn_executor(root: Path, job_id: str,
                    owner_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "pbatch.tui_pipeline_worker",
         "--workspace", str(root), "--job-id", job_id,
         "--owner-id", owner_id, "--execute"],
        cwd=str(root), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True)


def _spawn_executor_or_defer(
        store: TuiPipelineJobStore, root: Path, job_id: str,
        owner_id: str) -> Optional[subprocess.Popen]:
    try:
        return _spawn_executor(root, job_id, owner_id)
    except OSError as exc:
        store.requeue(
            job_id, retry_after=int(time.time()) + 5,
            progress=_handoff_text(f"executor launch deferred: {exc}", 256))
        return None


def _release_supervised_handoff(store: TuiPipelineJobStore,
                                job: Optional[dict]) -> None:
    if not job or job.get("state") != "handoff_pending":
        return
    if job.get("owner_id") == store.owner_id:
        claimed = store.claim_handoff(job["job_id"])
        store.release_handoff(claimed["job_id"])


def _execute(workspace_value: str, job_id: str, owner_id: str) -> int:
    root = Path(workspace_value).expanduser().resolve(strict=True)
    store = TuiPipelineJobStore(root, owner_id=owner_id)
    job = store.adopt_run(job_id)
    if store.cancel_requested(job_id):
        store.finish(job_id, "cancelled")
        return 0
    owner = _worker_owner(root, job)
    try:
        path = _job_pipeline_path(root, job)
        pipeline = load_pipeline(str(path))
        _prepare_pipeline(pipeline, root, owner)
        if any(bool(getattr(stage, "approval", False))
               for stage in getattr(pipeline, "stages", ())):
            raise ValueError(
                "persistent pipelines cannot contain interactive approval stages")
    except SystemExit as exc:
        return _save_failure(store, job, root,
                             f"pipeline definition rejected (exit {_exit_code(exc)})")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _save_failure(store, job, root, str(exc))
    return _run_loaded_pipeline(store, job, owner, pipeline, root)


def _job_pipeline_path(root: Path, job: dict[str, Any]) -> Path:
    path = (root / job["pipeline"]).resolve(strict=True)
    path.relative_to(root)
    if not path.is_file() or pipeline_fingerprint(path) != job["fingerprint"]:
        raise PipelineJobStoreError("persistent pipeline definition changed")
    return path


def _worker_owner(root: Path, job: dict[str, Any]) -> _WorkerOwner:
    settings = job["execution"]
    options = SimpleNamespace(
        cwd=str(root), agent=settings["agent"], provider=settings["provider"],
        timeout=settings["timeout"], memory_mode=settings["memory_mode"],
        session_name=job["session_name"], model="", orchestrator_only=True)
    return _WorkerOwner(options)


def _run_loaded_pipeline(store: TuiPipelineJobStore, job: dict[str, Any],
                         owner: _WorkerOwner, pipeline: Any, root: Path) -> int:
    channel = NativePipelineControlChannel()
    stop = threading.Event()
    watcher = threading.Thread(
        target=_watch_cancel, args=(store, job["job_id"], channel, stop),
        daemon=True, name="tui-pipeline-cancel-watch")
    watcher.start()
    results: list = []
    failed: list[str] = []
    error: Optional[Exception] = None
    lock_path = None
    try:
        with _pipeline_runtime_config(owner):
            try:
                lock_path = acquire_lock(_pipeline_cwd(pipeline))
            except SystemExit as exc:
                if store.cancel_requested(job["job_id"]):
                    store.finish(job["job_id"], "cancelled")
                    return 0
                return _defer_locked_job(store, job["job_id"], exc)
            if store.cancel_requested(job["job_id"]):
                store.finish(job["job_id"], "cancelled")
                return 0
            results, failed, error = _execute_pipeline(
                pipeline, store, job, owner, channel)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        error = exc
    finally:
        stop.set()
        watcher.join(timeout=1)
        release_lock(lock_path)
    if channel.cancelled:
        store.finish(job["job_id"], "cancelled")
        return 0
    return _save_results(store, job, root, results, failed, error)


def _execute_pipeline(pipeline: Any, store: TuiPipelineJobStore,
                      job: dict[str, Any], owner: _WorkerOwner,
                      channel: NativePipelineControlChannel
                      ) -> tuple[list, list[str], Optional[Exception]]:
    results: list = []
    failed: list[str] = []
    error: Optional[Exception] = None
    try:
        results, failed = run_pipeline(
            pipeline, model_override="",
            reuse=bool(job["reuse"] or job["attempts"] > 1),
            timeout_override=int(owner.options.timeout or 0),
            session_mode="new", session_name=_pipeline_session_name(owner),
            event_sink=lambda event: _record_progress(store, job, event, channel),
            approval_channel=channel,
        )
    except SystemExit as exc:
        error = RuntimeError(f"pipeline exited (exit {_exit_code(exc)})")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        error = exc
    return results, failed, error


def _record_progress(store: TuiPipelineJobStore, job: dict[str, Any],
                     event: Any, channel: NativePipelineControlChannel) -> None:
    kind = str(getattr(event, "kind", ""))
    if kind not in {"pipeline.started", "stage.started", "stage.completed"}:
        return
    payload = getattr(event, "payload", {})
    label = payload.get("stage", "") if isinstance(payload, dict) else ""
    text = _handoff_text(f"{kind}: {label}" if label else kind, 256)
    try:
        store.set_progress(job["job_id"], text)
    except (OSError, RuntimeError, TypeError, ValueError):
        channel.cancel_current_run()


def _watch_cancel(store: TuiPipelineJobStore, job_id: str,
                  channel: NativePipelineControlChannel,
                  stop: threading.Event) -> None:
    while not stop.wait(0.25):
        try:
            if store.cancel_requested(job_id):
                if channel.cancel_current_run():
                    return
                stop.wait(0.1)
                continue
        except (OSError, RuntimeError, TypeError, ValueError):
            channel.cancel_current_run()
            return


def _defer_locked_job(store: TuiPipelineJobStore, job_id: str,
                      error: SystemExit) -> int:
    store.requeue_supervised(
        job_id, retry_after=int(time.time()) + 5,
        progress=f"workspace lock unavailable (exit {_exit_code(error)})")
    return 0


def _save_failure(store: TuiPipelineJobStore, job: dict[str, Any],
                  root: Path, message: str) -> int:
    return _save_results(store, job, root, [], [], RuntimeError(message))


def _save_results(store: TuiPipelineJobStore, job: dict[str, Any], root: Path,
                  results: list, failed: list[str],
                  error: Optional[Exception]) -> int:
    try:
        details, _, _ = _pipeline_handoff_data(results, failed, error, root)
        store.save_handoff(job["job_id"], details)
        store.release_handoff(job["job_id"])
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        config.log.error("unable to persist pipeline result: %s", exc)
        return 1


def _exit_code(exc: SystemExit) -> int:
    return int(exc.code) if isinstance(exc.code, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
