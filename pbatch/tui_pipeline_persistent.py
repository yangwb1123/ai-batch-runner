"""Persistent-job controls mixed into the managed TUI pipeline runner."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from .tui_pipeline_handoff import (
    _display_path, _pipeline_handoff_prompt_from_record,
)
from .tui_pipeline_jobs import (
    MAX_RUN_ATTEMPTS, PipelineJobBusy, PipelineJobStoreError,
    TuiPipelineJobStore, _pid_alive,
)
from .tui_pipeline_support import (
    _defer_persistent_job, _persistent_job_identity_matches,
    _pipeline_job_store, _pipeline_path, _spawn_pipeline_worker,
    _workspace_root,
)
from .tui_view_support import one_line


class PersistentPipelineMixin:
    """Lifecycle and recovery methods for opt-in detached jobs."""

    def _launch_persistent_pipeline(
            self, root: Path, path: Path,
            store: Optional[TuiPipelineJobStore], job: Optional[dict]) -> bool:
        if store is None or job is None:
            self._append_entry("error", "Persistent pipeline job was not created.")
            return False
        try:
            job = store.claim_run(job["job_id"])
            worker = _spawn_pipeline_worker(root, job["job_id"], store.owner_id)
            self._persistent_worker_process = worker
            store.record_worker_pid(job["job_id"], worker.pid)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            try:
                store.requeue(
                    job["job_id"], retry_after=int(time.time()) + 5,
                    progress="worker launch failed")
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
            self._append_entry("error", f"Unable to launch pipeline worker: {exc}")
            return False
        self._tui_pipeline_job_store = store
        self._tui_pipeline_job = job
        self._persistent_pipeline_job_id = job["job_id"]
        self._persistent_pipeline_progress = "worker starting"
        self._last_status = "Persistent pipeline running"
        self._append_entry(
            "system", f"Pipeline detached as job `{job['job_id'][:12]}`: "
            f"`{_display_path(path)}`. It continues if this TUI closes.")
        self._refresh_status()
        return True

    def _poll_persistent_pipeline_job(self) -> None:
        if (not getattr(self.options, "persistent_pipelines", False)
                or not self.options.orchestrator_only):
            return
        process = self._persistent_worker_process
        if process is not None and process.poll() is not None:
            self._persistent_worker_process = None
        try:
            root = _workspace_root(self)
            store = _pipeline_job_store(self, root)
            job = store.read()
            if not job:
                return
            if job["state"] in {"completed", "failed", "cancelled"}:
                self._finish_persistent_status(job)
                return
            self._persistent_pipeline_job_id = job["job_id"]
            if not _persistent_job_identity_matches(self, root, job):
                self._persistent_notice(
                    "Persistent pipeline identity changed; recovery is blocked.")
                return
            if job["state"] == "handoff_pending":
                self._poll_pending_handoff(store, root, job)
            elif job["state"] == "queued":
                self._poll_queued_job(store, job)
            elif job["state"] == "running":
                self._poll_running_job(store, job)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._persistent_notice(f"Persistent pipeline monitor blocked: {exc}")

    def _poll_pending_handoff(self, store: TuiPipelineJobStore,
                              root: Path, job: dict) -> None:
        if (not self._orchestrator_only_ready or self._busy
                or self._session_control_busy()
                or store.owned_by_other_live_process(job)):
            return
        self._deliver_persistent_handoff(store, root, job)

    def _deliver_persistent_handoff(self, store: TuiPipelineJobStore,
                                    root: Path, job: dict) -> None:
        claimed = store.claim_handoff(job["job_id"])
        self._tui_pipeline_job_store = store
        self._tui_pipeline_job = claimed
        prompt = _pipeline_handoff_prompt_from_record(claimed["handoff"], root)
        self._pipeline_handoff_job_id = claimed["job_id"]
        self.call_after_refresh(self._send_pipeline_handoff, prompt)

    def _fail_exhausted_persistent_job(self, store: TuiPipelineJobStore,
                                       job: dict) -> None:
        try:
            failed = store.fail_orphaned_exhausted(job["job_id"])
        except PipelineJobBusy:
            self._persistent_notice("Pipeline supervisor or worker is still active.")
            return
        self._persistent_notice(failed["progress"])
        self._poll_pending_handoff(store, store.workspace, failed)

    def _poll_queued_job(self, store: TuiPipelineJobStore,
                         job: dict) -> None:
        if job["cancel_requested"]:
            cancelled = store.finish_queued_cancel(job["job_id"])
            self._finish_persistent_status(cancelled)
            return
        if store.owned_by_other_live_process(job):
            self._persistent_notice(job["progress"])
            return
        if job["attempts"] >= MAX_RUN_ATTEMPTS:
            self._fail_exhausted_persistent_job(store, job)
            return
        if job["retry_after"] > int(time.time()):
            self._persistent_notice(job["progress"])
            return
        self._start_pipeline_path(job["pipeline"], reuse=True, resume_job=job)

    def _poll_running_job(self, store: TuiPipelineJobStore,
                          job: dict) -> None:
        if job["worker_pid"] and _pid_alive(job["worker_pid"]):
            self._persistent_notice(job["progress"])
            return
        if store.owned_by_other_live_process(job):
            self._persistent_notice(job["progress"])
            return
        if job["cancel_requested"]:
            cancelled = store.finish_abandoned_cancel(job["job_id"])
            self._finish_persistent_status(cancelled)
            return
        if job["attempts"] >= MAX_RUN_ATTEMPTS:
            self._fail_exhausted_persistent_job(store, job)
            return
        if job["worker_pid"]:
            self._start_pipeline_path(job["pipeline"], reuse=True, resume_job=job)
            return
        if (job["owner_pid"] == os.getpid() and job["owner_id"] == store.owner_id
                and int(time.time()) - job["updated_at"] < 10):
            return
        if int(time.time()) - job["updated_at"] < 2:
            return
        self._start_pipeline_path(job["pipeline"], reuse=True, resume_job=job)

    def _cancel_persistent_pipeline(self) -> bool:
        store = getattr(self, "_tui_pipeline_job_store", None)
        job_id = str(getattr(self, "_persistent_pipeline_job_id", "") or "")
        if store is None or not job_id:
            return False
        try:
            store.request_cancel(job_id)
            self._persistent_notice("Persistent pipeline cancellation requested.")
            return True
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._append_entry("error", f"Unable to cancel pipeline job: {exc}")
            return False

    def _finish_persistent_status(self, job: dict) -> None:
        if job["job_id"] != self._persistent_pipeline_job_id:
            return
        self._persistent_notice(f"Persistent pipeline {job['state']}.")
        self._persistent_pipeline_job_id = ""
        self._persistent_pipeline_progress = ""
        self._tui_pipeline_job = None

    def _persistent_notice(self, message: str) -> None:
        text = one_line(str(message))[:256]
        if not text or text == self._persistent_pipeline_progress:
            return
        self._persistent_pipeline_progress = text
        self._append_entry("system", text)
        self._last_status = text
        self._refresh_status()

    def _recover_pipeline_job_or_start(
            self, requested: str = "", *, reuse_if_new: bool = False) -> None:
        if not self.options.orchestrator_only:
            if requested:
                self._start_pipeline_path(requested)
            return
        try:
            root = _workspace_root(self)
            store = _pipeline_job_store(self, root)
            job = store.active()
            if job is None:
                if requested:
                    self._start_pipeline_path(requested, reuse=reuse_if_new)
                return
            self._recover_active_pipeline_job(store, root, job, requested)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._append_entry("error", f"Pipeline recovery blocked: {exc}")

    def _recover_active_pipeline_job(self, store: TuiPipelineJobStore,
                                    root: Path, job: dict,
                                    requested: str) -> None:
        path = _pipeline_path(job["pipeline"], root)
        if not _persistent_job_identity_matches(self, root, job):
            raise PipelineJobStoreError(
                "pipeline or controller identity changed; resume is blocked")
        if requested and _pipeline_path(requested, root) != path:
            raise PipelineJobBusy("another pipeline job is awaiting recovery")
        self._tui_pipeline_job_store = store
        self._persistent_pipeline_job_id = job["job_id"]
        owner_is_live = store.owned_by_other_live_process(job)
        if _defer_persistent_job(self, store, job, owner_is_live):
            return
        if owner_is_live:
            raise PipelineJobBusy("an active TUI process owns this pipeline job")
        if job["state"] in {"queued", "running"} and job["cancel_requested"]:
            if job["state"] == "queued":
                cancelled = store.finish_queued_cancel(job["job_id"])
            else:
                cancelled = store.finish_abandoned_cancel(job["job_id"])
            self._finish_persistent_status(cancelled)
            return
        if job["state"] == "handoff_pending":
            self._deliver_persistent_handoff(store, root, job)
        elif job["attempts"] >= MAX_RUN_ATTEMPTS:
            self._fail_exhausted_persistent_job(store, job)
        else:
            self._start_pipeline_path(job["pipeline"], reuse=True, resume_job=job)
