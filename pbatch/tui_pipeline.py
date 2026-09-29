"""Interactive entrypoint for read-only observed native Pipelines.

The TUI may start a pipeline only through an explicit command or option.  The
pipeline remains the execution authority; this mixin validates the local
request, attaches a read-only event projection, exposes only the scoped native
control channel, and renders the final runner outcome.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from . import config
from .lock import acquire_lock, release_lock
from .pipeline import load_pipeline, run_pipeline
from .tui_native import (
    NativePipelineControlChannel, NativePipelineAttachment)
from .tui_pipeline_handoff import (
    _display_path, _format_pipeline_handoff, _handoff_text,
    _pipeline_handoff_data, _pipeline_handoff_prompt, _pipeline_lock_refused,
)
from .tui_pipeline_jobs import (
    PipelineJobBusy, PipelineJobStoreError, TuiPipelineJobStore,
)
from .tui_pipeline_persistent import PersistentPipelineMixin
from .tui_pipeline_support import (
    PipelineWorkspaceLockRefused, _controller_fingerprint, _exit_code,
    _pipeline_cwd,
    _pipeline_execution_settings, _pipeline_runtime_config,
    _pipeline_session_name, _prepare_pipeline, _prepare_pipeline_request,
    _spawn_pipeline_worker, _system_exit_error, _workspace_root,
)
from .tui_view_support import result_fields


class TUIPipelineMixin(PersistentPipelineMixin):
    """Run one explicit Pipeline while keeping TUI controls read-only."""

    _pipeline_busy = False
    _pipeline_source = ""
    _pipeline_failed_stages: tuple[str, ...] = ()
    _pipeline_result_count = 0
    _persistent_pipeline_job_id = ""
    _persistent_pipeline_progress = ""
    _persistent_pipeline_timer = None
    _persistent_worker_process = None

    def _schedule_pipeline_startup(self) -> None:
        if self.options.native_session:
            return
        if (getattr(self.options, "persistent_pipelines", False)
                and self.options.orchestrator_only):
            self._persistent_pipeline_timer = self.set_interval(
                0.75, self._poll_persistent_pipeline_job)
        if self.options.orchestrator_only:
            self.call_after_refresh(
                self._recover_pipeline_job_or_start, self.options.pipeline)
        elif self.options.pipeline:
            self.call_after_refresh(
                self._start_pipeline_path, self.options.pipeline)

    def _slash_pipeline(self, args: tuple[str, ...]) -> None:
        if len(args) not in (1, 2) or (len(args) == 2 and args[1] != "--reuse"):
            self._usage("pipeline", "<file> [--reuse]")
            return
        reuse = len(args) == 2
        if self.options.orchestrator_only and reuse:
            self._recover_pipeline_job_or_start(args[0], reuse_if_new=True)
        else:
            self._start_pipeline_path(args[0], reuse=reuse)

    def _palette_pipeline(self) -> None:
        self._slash_pipeline(())

    def _start_pipeline_path(self, raw_path: str, *, reuse: bool = False,
                             resume_job: Optional[dict] = None) -> bool:
        if self._pipeline_start_blocked():
            return False
        try:
            request = _prepare_pipeline_request(self, raw_path, resume_job)
        except (OSError, RuntimeError, SystemExit, TypeError, ValueError) as exc:
            self._append_entry("error", f"Unable to start pipeline: {exc}")
            return False
        return self._dispatch_pipeline_request(request, reuse)

    def _pipeline_start_blocked(self) -> bool:
        if (getattr(self.options, "persistent_pipelines", False)
                and not self.options.orchestrator_only):
            self._append_entry(
                "error", "Persistent pipelines require orchestrator-only mode.")
            return True
        if (self.options.orchestrator_only
                and not self._orchestrator_only_ready):
            self._append_entry(
                "error", "Pipeline handoff requires a Pi RPC session with tools disabled.")
            return True
        if self._busy or self._session_control_busy():
            self.notify("Wait for the active operation before starting a pipeline.",
                        severity="warning")
            return True
        if self._native_pipeline_is_running():
            self.notify("Wait for the attached native pipeline to finish.",
                        severity="warning")
            return True
        return False

    def _dispatch_pipeline_request(self, request: tuple, reuse: bool) -> bool:
        root, path, pipeline, store, job, fingerprint = request
        persistent = bool(getattr(self.options, "persistent_pipelines", False))
        attachment = self._pipeline_attachment(root, persistent)
        if attachment is False:
            return False
        if store is not None and job is None:
            try:
                job = store.create(
                    path.relative_to(root).as_posix(), fingerprint,
                    str(self.options.session_name or "tui"),
                    _controller_fingerprint(self), reuse=reuse,
                    execution=_pipeline_execution_settings(self))
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                if attachment is not None:
                    self.detach_native_pipeline()
                self._append_entry("error", f"Unable to persist pipeline job: {exc}")
                return False
        if persistent:
            return self._launch_persistent_pipeline(root, path, store, job)
        return self._launch_pipeline(
            pipeline, path, attachment, reuse, store=store, job=job)

    def _pipeline_attachment(self, root: Path, persistent: bool):
        if persistent:
            return None
        if self._native_pipeline_attachment is not None:
            self.detach_native_pipeline()
        try:
            return self.attach_native_pipeline(root)
        except (RuntimeError, TypeError, ValueError) as exc:
            self._append_entry("error", f"Unable to attach pipeline: {exc}")
            return False

    def _launch_pipeline(self, pipeline: Any, path: Path,
                         attachment: NativePipelineAttachment, reuse: bool, *,
                         store: Optional[TuiPipelineJobStore] = None,
                         job: Optional[dict] = None) -> bool:
        if store is not None and job is not None:
            try:
                job = store.claim_run(job["job_id"])
            except (PipelineJobBusy, PipelineJobStoreError, OSError) as exc:
                self.detach_native_pipeline()
                self._append_entry("error", f"Unable to claim pipeline job: {exc}")
                return False
            self._tui_pipeline_job_store = store
            self._tui_pipeline_job = job
        self._pipeline_busy = True
        self._native_pipeline_approval = NativePipelineControlChannel()
        self._pipeline_source = str(path)
        self._pipeline_failed_stages = ()
        self._pipeline_result_count = 0
        self._set_busy(True)
        composer = self.query_one("#composer")
        composer.placeholder = "Native pipeline running (read-only)…"
        self._mark_agent_operation_started("pipeline", str(path))
        self._append_entry(
            "system", f"Starting native Pipeline: `{_display_path(path)}`"
            + (" (reuse)" if reuse else ""))
        try:
            approval_channel = self._native_pipeline_approval
            self._worker = self.run_worker(
                lambda: self._run_pipeline_worker(
                    pipeline, attachment, reuse, approval_channel),
                name="native-pipeline", group="agent", exit_on_error=False,
                exclusive=True, thread=True)
        except Exception as exc:
            self._pipeline_busy = False
            if self._native_pipeline_approval is not None:
                self._native_pipeline_approval.close("pipeline worker could not start")
            self.detach_native_pipeline()
            self._set_busy(False)
            if store is not None and job is not None:
                try:
                    store.finish(job["job_id"], "failed")
                except (PipelineJobStoreError, OSError):
                    pass
                self._tui_pipeline_job = None
            self._append_entry("error", f"Unable to run pipeline: {exc}")
            return False
        return True

    def _run_pipeline_worker(self, pipeline: Any,
                             attachment: NativePipelineAttachment,
                             reuse: bool,
                             approval_channel: Optional[NativePipelineControlChannel]) -> None:
        results: list = []
        failed: list[str] = []
        error: Optional[Exception] = None
        cancelled = False
        lock_path = None
        try:
            with _pipeline_runtime_config(self):
                session_name = _pipeline_session_name(self)
                # A TUI pipeline deliberately takes the conservative exclusive
                # workspace lock: dynamic pipeline sources cannot declare every
                # eventual output before execution.
                try:
                    lock_path = acquire_lock(_pipeline_cwd(pipeline))
                except SystemExit as exc:
                    error = PipelineWorkspaceLockRefused(
                        f"pipeline workspace lock refused (exit "
                        f"{_exit_code(exc)})")
                else:
                    try:
                        results, failed = run_pipeline(
                            pipeline,
                            model_override=("" if self.options.orchestrator_only else
                                            str(getattr(self.options, "model", "") or "")),
                            reuse=reuse,
                            timeout_override=int(
                                getattr(self.options, "timeout", 0) or 0),
                            session_mode="new", session_name=session_name,
                            event_sink=attachment,
                            approval_channel=approval_channel,
                        )
                    except SystemExit as exc:
                        error = _system_exit_error(exc, "native pipeline exited")
        except Exception as exc:
            error = exc
        finally:
            release_lock(lock_path)
        cancelled = bool(getattr(approval_channel, "cancelled", False))
        try:
            self.call_from_thread(
                self._finish_pipeline, results, failed, error, cancelled)
        except RuntimeError:
            pass

    def _finish_pipeline(self, results: list, failed: list[str],
                         error: Optional[Exception], cancelled: bool = False) -> None:
        self._pipeline_busy = False
        self._native_pipeline_approval = None
        self._pipeline_result_count = min(len(results), 4096)
        self._pipeline_failed_stages = tuple(str(item)[:256] for item in failed[:32])
        if error is not None:
            self._last_status = "Pipeline failed"
            self._append_entry("error", f"Native Pipeline failed: {error}")
        elif cancelled:
            self._last_status = "Pipeline interrupted"
            self._append_entry("system", self._last_status)
        elif failed:
            self._last_status = "Pipeline failed"
            names = ", ".join(f"`{name}`" for name in self._pipeline_failed_stages)
            self._append_entry(
                "error", f"Native Pipeline stopped; failed stage(s): {names}")
        else:
            self._last_status = (
                f"Pipeline completed ({self._pipeline_result_count} task result(s))")
            self._append_entry("system", self._last_status)
        self._worker = None
        self._set_busy(False)
        self._mark_agent_operation_finished()
        self._refresh_completion_inspector()
        if self.options.orchestrator_only:
            prompt = self._persist_pipeline_outcome(
                results, failed, error, cancelled)
            if prompt:
                self.call_after_refresh(self._send_pipeline_handoff, prompt)

    def _persist_pipeline_outcome(self, results: list, failed: list[str],
                                  error: Optional[Exception], cancelled: bool
                                  ) -> str:
        job = getattr(self, "_tui_pipeline_job", None)
        store = getattr(self, "_tui_pipeline_job_store", None)
        if not job or store is None:
            return _pipeline_handoff_prompt(results, failed, error,
                                            _workspace_root(self))
        job_id = job["job_id"]
        try:
            if cancelled:
                store.finish(job_id, "cancelled")
                self._tui_pipeline_job = None
                return ""
            if _pipeline_lock_refused(error):
                store.finish(job_id, "failed")
                self._tui_pipeline_job = None
                self._append_entry(
                    "system", "Pipeline stopped: workspace is locked by another run.")
                return ""
            root = _workspace_root(self)
            handoff, artifacts, inline = _pipeline_handoff_data(
                results, failed, error, root)
            job = store.save_handoff(job_id, handoff)
            self._tui_pipeline_job = job
            self._pipeline_handoff_job_id = job_id
            return _format_pipeline_handoff(handoff, artifacts, inline)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._append_entry("error", f"Unable to persist pipeline outcome: {exc}")
            return _pipeline_handoff_prompt(results, failed, error,
                                            _workspace_root(self))

    def _send_pipeline_handoff(self, prompt: str) -> None:
        job = getattr(self, "_tui_pipeline_job", None)
        if job and job.get("state") == "handoff_pending":
            self._pipeline_handoff_job_id = job["job_id"]
        if not prompt:
            self._release_pending_handoff()
            return
        if (not self._orchestrator_only_ready or not self._start_turn(prompt)):
            self._release_pending_handoff()
            self._append_entry(
                "system", "Pipeline results are ready; automatic review could not start.")
        else:
            self._pipeline_handoff_inflight = True

    def _release_pending_handoff(self) -> None:
        job = getattr(self, "_tui_pipeline_job", None)
        store = getattr(self, "_tui_pipeline_job_store", None)
        if job and store:
            try:
                store.release_handoff(job["job_id"])
            except (PipelineJobStoreError, OSError):
                pass
        self._pipeline_handoff_job_id = ""
        self._pipeline_handoff_inflight = False

    def _complete_pipeline_handoff_turn(self, result: Any,
                                        error: Optional[Exception]) -> None:
        job_id = str(getattr(self, "_pipeline_handoff_job_id", "") or "")
        if not job_id:
            return
        success = False
        if error is None:
            try:
                success = bool(result_fields(result)[0])
            except (TypeError, ValueError):
                success = False
        if success:
            store = getattr(self, "_tui_pipeline_job_store", None)
            job = getattr(self, "_tui_pipeline_job", None)
            outcome = "completed"
            if job and job.get("handoff", {}).get("outcome") == "failed":
                outcome = "failed"
            if store is not None:
                try:
                    store.finish(job_id, outcome)
                except (PipelineJobStoreError, OSError):
                    pass
            self._tui_pipeline_job = None
            if self._persistent_pipeline_job_id == job_id:
                self._persistent_pipeline_job_id = ""
                self._persistent_pipeline_progress = ""
        self._pipeline_handoff_job_id = ""
        self._pipeline_handoff_inflight = False


__all__ = ["TUIPipelineMixin"]
