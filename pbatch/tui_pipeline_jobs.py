"""Private, bounded persistence for resumable TUI pipeline jobs.

The store contains pipeline identity, lifecycle state, artifact references,
and a bounded stdout-only handoff excerpt. It never stores prompts or artifact
bodies; execution remains owned by ``run_pipeline`` and its workspace lock.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import uuid
from typing import Any, Iterator, Optional

from . import config, file_lock
from .tui_pipeline_job_state import (
    MAX_HANDOFF_TASKS, MAX_RUN_ATTEMPTS, MAX_STATE_BYTES, SCHEMA_VERSION,
    PipelineJobStoreError, _ACTIVE_STATES, _TERMINAL_STATES,
    _bounded_int, _bounded_text,
    _digest, _execution, _handoff, _now, _open_private, _pid_alive,
    _read_bounded, _relative_path, _valid_job_id, _validate_job,
    _validate_state, _write_all,
    pipeline_fingerprint,
)


class PipelineJobBusy(PipelineJobStoreError):
    """A different live TUI owner already holds the job."""


class TuiPipelineJobStore:
    """Single-active-job store scoped to one canonical workspace."""

    def __init__(self, workspace: str | os.PathLike[str], *, owner_id: str = ""):
        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise PipelineJobStoreError("pipeline job workspace is unavailable")
        runtime = config.data_dir(str(self.workspace)).resolve()
        self.directory = runtime / "tui" / "pipeline-jobs"
        self.state_path = self.directory / "state.json"
        self.lock_path = self.directory / "state.lock"
        self.owner_id = owner_id or uuid.uuid4().hex
        _bounded_text(self.owner_id, "owner_id", 64)
        self.owner_pid = os.getpid()
        self._thread_lock = threading.RLock()

    def read(self) -> Optional[dict[str, Any]]:
        with self._locked():
            return self._read_state()["job"]

    def active(self) -> Optional[dict[str, Any]]:
        job = self.read()
        return job if job and job["state"] in _ACTIVE_STATES else None

    def owned_by_other_live_process(self, job: dict[str, Any]) -> bool:
        worker_pid = job["worker_pid"]
        if worker_pid and worker_pid != self.owner_pid and _pid_alive(worker_pid):
            return True
        pid = job["owner_pid"]
        if not pid or (pid == self.owner_pid and job["owner_id"] == self.owner_id):
            return False
        return _pid_alive(pid)

    def create(self, pipeline: str, fingerprint: str, session_name: str,
               controller_fingerprint: str = "", *, reuse: bool = False,
               execution: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        relative = _relative_path(pipeline, "pipeline")
        _digest(fingerprint, "pipeline fingerprint")
        controller_fingerprint = (controller_fingerprint or
                                  hashlib.sha256(b"").hexdigest())
        _digest(controller_fingerprint, "controller fingerprint")
        _bounded_text(session_name, "session_name", 160)
        execution = _execution(execution or {
            "agent": "", "provider": "", "timeout": 0,
            "memory_mode": "auto",
        })
        if not isinstance(reuse, bool):
            raise PipelineJobStoreError("pipeline reuse setting is invalid")
        with self._locked():
            state = self._read_state()
            previous = state["job"]
            if previous and previous["state"] in _ACTIVE_STATES:
                raise PipelineJobBusy("an unfinished pipeline job already exists")
            now = _now()
            job = {
                "job_id": uuid.uuid4().hex, "pipeline": relative,
                "fingerprint": fingerprint,
                "controller_fingerprint": controller_fingerprint,
                "session_name": session_name,
                "state": "queued", "attempts": 0, "owner_pid": 0,
                "worker_pid": 0,
                "owner_id": "", "created_at": now, "updated_at": now,
                "handoff": None, "reuse": reuse, "cancel_requested": False,
                "progress": "queued", "retry_after": 0,
                "execution": execution,
            }
            state["job"] = job
            self._write_state(state)
            return dict(job)

    def claim_run(self, job_id: str) -> dict[str, Any]:
        def update(job: dict[str, Any]) -> None:
            if job["state"] not in {"queued", "running"}:
                raise PipelineJobStoreError("pipeline job is not resumable")
            if job["cancel_requested"]:
                raise PipelineJobBusy("pipeline cancellation is pending")
            if job["attempts"] >= MAX_RUN_ATTEMPTS:
                raise PipelineJobStoreError("pipeline recovery attempt budget exhausted")
            if job["retry_after"] > _now():
                raise PipelineJobBusy("pipeline retry is deferred")
            self._claim(job)
            job["state"] = "running"
            job["attempts"] += 1
            job["worker_pid"] = 0
            job["retry_after"] = 0
            job["cancel_requested"] = False
        return self._update(job_id, update)

    def adopt_supervisor(self, job_id: str) -> dict[str, Any]:
        def update(job: dict[str, Any]) -> None:
            if (job["state"] != "running" or job["owner_id"] != self.owner_id
                    or job["worker_pid"] not in {0, self.owner_pid}):
                raise PipelineJobBusy("pipeline supervisor ownership changed")
            job["owner_pid"] = self.owner_pid
            job["worker_pid"] = self.owner_pid
            job["progress"] = "persistent supervisor active"
        return self._update(job_id, update)

    def adopt_run(self, job_id: str) -> dict[str, Any]:
        def update(job: dict[str, Any]) -> None:
            if (job["state"] != "running" or job["owner_id"] != self.owner_id
                    or job["worker_pid"] not in {0, job["owner_pid"], self.owner_pid}):
                raise PipelineJobBusy("pipeline worker launch ownership changed")
            supervisor_pid = job["worker_pid"] or job["owner_pid"]
            job["owner_pid"] = self.owner_pid
            job["worker_pid"] = supervisor_pid
        return self._update(job_id, update)

    def record_worker_pid(self, job_id: str, worker_pid: int) -> dict[str, Any]:
        _bounded_int(worker_pid, "worker_pid", 1, 2**31 - 1)

        def update(job: dict[str, Any]) -> None:
            if job["state"] != "running":
                return
            if job["owner_id"] != self.owner_id:
                raise PipelineJobBusy("pipeline worker ownership changed")
            if job["owner_pid"] == worker_pid:
                return
            self._require_owner(job)
            job["worker_pid"] = worker_pid
        return self._update(job_id, update)

    def set_progress(self, job_id: str, progress: str) -> dict[str, Any]:
        value = _bounded_text(progress, "progress", 256)

        def update(job: dict[str, Any]) -> None:
            self._require_owner(job)
            if job["state"] != "running":
                raise PipelineJobStoreError("pipeline job is not running")
            job["progress"] = value
        return self._update(job_id, update)

    def request_cancel(self, job_id: str) -> bool:
        def update(job: dict[str, Any]) -> None:
            if job["state"] not in {"queued", "running"}:
                raise PipelineJobStoreError("pipeline job cannot be cancelled now")
            job["cancel_requested"] = True
            job["progress"] = "cancellation requested"
        self._update(job_id, update)
        return True

    def finish_queued_cancel(self, job_id: str) -> dict[str, Any]:
        def update(job: dict[str, Any]) -> None:
            if job["state"] != "queued" or not job["cancel_requested"]:
                raise PipelineJobStoreError("queued pipeline cancellation is not pending")
            job["state"] = "cancelled"
            job["progress"] = "cancelled"
            job["cancel_requested"] = False
            job["retry_after"] = 0
            job["owner_pid"] = 0
            job["worker_pid"] = 0
            job["owner_id"] = ""
        return self._update(job_id, update)

    def finish_supervised_cancel(self, job_id: str) -> dict[str, Any]:
        def update(job: dict[str, Any]) -> None:
            if (job["state"] != "running" or not job["cancel_requested"]
                    or job["owner_id"] != self.owner_id
                    or job["worker_pid"] != self.owner_pid):
                raise PipelineJobStoreError("supervised cancellation is not pending")
            if (job["owner_pid"] != self.owner_pid
                    and _pid_alive(job["owner_pid"])):
                raise PipelineJobBusy("pipeline worker is still running")
            job["state"] = "cancelled"
            job["progress"] = "cancelled"
            job["cancel_requested"] = False
            job["owner_pid"] = 0
            job["worker_pid"] = 0
            job["owner_id"] = ""
        return self._update(job_id, update)

    def fail_exhausted(self, job_id: str, message: str) -> dict[str, Any]:
        error = _bounded_text(message, "supervisor error", 512)

        def update(job: dict[str, Any]) -> None:
            if job["attempts"] < MAX_RUN_ATTEMPTS:
                raise PipelineJobStoreError("pipeline attempt budget is not exhausted")
            if job["state"] == "running":
                if (job["owner_id"] != self.owner_id
                        or job["worker_pid"] != self.owner_pid
                        or (job["owner_pid"] != self.owner_pid
                            and _pid_alive(job["owner_pid"]))):
                    raise PipelineJobBusy("pipeline owner is still active")
            elif job["state"] == "queued":
                if (job["owner_id"] != self.owner_id
                        or job["owner_pid"] != self.owner_pid
                        or job["worker_pid"] != self.owner_pid):
                    raise PipelineJobStoreError("queued pipeline supervisor changed")
            else:
                raise PipelineJobStoreError("pipeline cannot fail from this state")
            job["handoff"] = _handoff({
                "outcome": "failed", "failed_stages": [], "error": error,
                "tasks": [], "omitted_task_results": 0, "inline": "",
            })
            job["state"] = "handoff_pending"
            job["progress"] = "worker recovery exhausted"
            job["cancel_requested"] = False
            job["owner_pid"] = 0
            job["worker_pid"] = 0
            job["owner_id"] = ""
        return self._update(job_id, update)

    def fail_orphaned_exhausted(self, job_id: str) -> dict[str, Any]:
        message = "worker recovery attempt budget exhausted"

        def update(job: dict[str, Any]) -> None:
            if job["attempts"] < MAX_RUN_ATTEMPTS:
                raise PipelineJobStoreError("pipeline attempt budget is not exhausted")
            if job["state"] not in {"running", "queued"}:
                raise PipelineJobStoreError("pipeline cannot fail from this state")
            if (_pid_alive(job["owner_pid"])
                    or _pid_alive(job["worker_pid"])):
                raise PipelineJobBusy("pipeline supervisor or worker is still active")
            job["handoff"] = _handoff({
                "outcome": "failed", "failed_stages": [], "error": message,
                "tasks": [], "omitted_task_results": 0, "inline": "",
            })
            job["state"] = "handoff_pending"
            job["progress"] = message
            job["cancel_requested"] = False
            job["owner_pid"] = 0
            job["worker_pid"] = 0
            job["owner_id"] = ""
        return self._update(job_id, update)

    def finish_abandoned_cancel(self, job_id: str) -> dict[str, Any]:
        def update(job: dict[str, Any]) -> None:
            if job["state"] != "running" or not job["cancel_requested"]:
                raise PipelineJobStoreError("abandoned cancellation is not pending")
            if (job["owner_pid"] != self.owner_pid
                    and _pid_alive(job["owner_pid"])):
                raise PipelineJobBusy("pipeline worker is still running")
            if job["worker_pid"] and _pid_alive(job["worker_pid"]):
                raise PipelineJobBusy("pipeline worker is still starting")
            job["state"] = "cancelled"
            job["progress"] = "cancelled"
            job["cancel_requested"] = False
            job["owner_pid"] = 0
            job["worker_pid"] = 0
            job["owner_id"] = ""
        return self._update(job_id, update)

    def cancel_requested(self, job_id: str) -> bool:
        job = self.read()
        return bool(job and job["job_id"] == job_id
                    and job["cancel_requested"])

    def claim_handoff(self, job_id: str) -> dict[str, Any]:
        def update(job: dict[str, Any]) -> None:
            if job["state"] != "handoff_pending" or not job["handoff"]:
                raise PipelineJobStoreError("pipeline job has no pending handoff")
            self._claim(job)
        return self._update(job_id, update)

    def save_handoff(self, job_id: str,
                     handoff: dict[str, Any]) -> dict[str, Any]:
        normalized = _handoff(handoff)

        def update(job: dict[str, Any]) -> None:
            self._require_owner(job)
            if job["state"] != "running":
                raise PipelineJobStoreError("pipeline job is not running")
            job["handoff"] = normalized
            job["state"] = "handoff_pending"
            job["progress"] = "result ready"
            job["cancel_requested"] = False
        return self._update(job_id, update)

    def finish(self, job_id: str, outcome: str) -> dict[str, Any]:
        if outcome not in _TERMINAL_STATES:
            raise PipelineJobStoreError("pipeline job terminal outcome is invalid")

        def update(job: dict[str, Any]) -> None:
            self._require_owner(job)
            if job["state"] not in {"running", "handoff_pending"}:
                raise PipelineJobStoreError("pipeline job cannot be finished")
            job["state"] = outcome
            job["progress"] = outcome
            job["cancel_requested"] = False
            job["owner_pid"] = 0
            job["worker_pid"] = 0
            job["owner_id"] = ""
        return self._update(job_id, update)

    def requeue_supervised(self, job_id: str, *, retry_after: int,
                           progress: str) -> dict[str, Any]:
        _bounded_int(retry_after, "retry_after", 0, 2**63 - 1)
        _bounded_text(progress, "progress", 256)

        def update(job: dict[str, Any]) -> None:
            self._require_owner(job)
            if (job["state"] != "running" or not job["worker_pid"]
                    or job["worker_pid"] == self.owner_pid):
                raise PipelineJobStoreError("job has no detached supervisor lease")
            supervisor_pid = job["worker_pid"]
            job["state"] = "queued"
            job["progress"] = progress
            job["retry_after"] = retry_after
            job["owner_pid"] = supervisor_pid
            job["worker_pid"] = supervisor_pid
        return self._update(job_id, update)

    def requeue(self, job_id: str, *, retry_after: int = 0,
                progress: str = "queued") -> dict[str, Any]:
        _bounded_int(retry_after, "retry_after", 0, 2**63 - 1)
        _bounded_text(progress, "progress", 256)

        def update(job: dict[str, Any]) -> None:
            self._require_owner(job)
            if job["state"] != "running":
                raise PipelineJobStoreError("only a running job can be requeued")
            job["state"] = "queued"
            job["progress"] = progress
            job["retry_after"] = retry_after
            job["owner_pid"] = 0
            job["worker_pid"] = 0
            job["owner_id"] = ""
        return self._update(job_id, update)

    def release_handoff(self, job_id: str) -> dict[str, Any]:
        def update(job: dict[str, Any]) -> None:
            self._require_owner(job)
            if job["state"] != "handoff_pending":
                raise PipelineJobStoreError("pipeline handoff is not pending")
            job["owner_pid"] = 0
            job["worker_pid"] = 0
            job["owner_id"] = ""
        return self._update(job_id, update)

    def _claim(self, job: dict[str, Any]) -> None:
        if job["owner_id"] != self.owner_id and (
                (job["owner_pid"] and _pid_alive(job["owner_pid"]))
                or (job["worker_pid"] and _pid_alive(job["worker_pid"]))):
            raise PipelineJobBusy("pipeline job is owned by another live TUI or worker")
        job["owner_pid"] = self.owner_pid
        job["owner_id"] = self.owner_id

    def _require_owner(self, job: dict[str, Any]) -> None:
        if (job["owner_pid"] != self.owner_pid
                or job["owner_id"] != self.owner_id):
            raise PipelineJobBusy("pipeline job ownership changed")

    def _update(self, job_id: str, change) -> dict[str, Any]:
        if not _valid_job_id(job_id):
            raise PipelineJobStoreError("pipeline job id is invalid")
        with self._locked():
            state = self._read_state()
            job = state["job"]
            if not job or job["job_id"] != job_id:
                raise PipelineJobStoreError("pipeline job was replaced")
            change(job)
            job["updated_at"] = _now()
            _validate_job(job)
            self._write_state(state)
            return dict(job)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self._ensure_directory()
            descriptor = _open_private(
                self.lock_path, os.O_RDWR | os.O_CREAT, create=True)
            try:
                file_lock.flock(descriptor, file_lock.LOCK_EX)
                yield
            finally:
                try:
                    file_lock.flock(descriptor, file_lock.LOCK_UN)
                finally:
                    os.close(descriptor)

    def _ensure_directory(self) -> None:
        runtime = self.directory.parent.parent
        for path in (runtime / "tui", self.directory):
            try:
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                info = path.lstat()
            except OSError as exc:
                raise PipelineJobStoreError(
                    f"pipeline job directory unavailable: {exc}") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise PipelineJobStoreError("pipeline job directory is unsafe")
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists() and not self.state_path.is_symlink():
            return {"schema_version": SCHEMA_VERSION,
                    "workspace": str(self.workspace), "job": None}
        descriptor = _open_private(self.state_path, os.O_RDONLY)
        try:
            info = os.fstat(descriptor)
            if info.st_size > MAX_STATE_BYTES:
                raise PipelineJobStoreError("pipeline job state exceeds size bound")
            raw = _read_bounded(descriptor, MAX_STATE_BYTES)
        finally:
            os.close(descriptor)
        try:
            state = json.loads(raw.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PipelineJobStoreError("pipeline job state is invalid JSON") from exc
        if (not isinstance(state, dict)
                or set(state) != {"schema_version", "workspace", "job"}
                or state["schema_version"] != SCHEMA_VERSION
                or state["workspace"] != str(self.workspace)):
            raise PipelineJobStoreError("pipeline job state scope/schema mismatch")
        if state["job"] is not None:
            _validate_job(state["job"])
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        _validate_state(state, self.workspace)
        raw = json.dumps(state, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > MAX_STATE_BYTES:
            raise PipelineJobStoreError("pipeline job state exceeds size bound")
        temporary = self.directory / f".state-{uuid.uuid4().hex}.tmp"
        descriptor = -1
        try:
            descriptor = _open_private(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create=True)
            _write_all(descriptor, raw)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.state_path)
            directory = os.open(
                self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise PipelineJobStoreError(f"cannot persist pipeline job: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass




__all__ = [
    "PipelineJobBusy", "PipelineJobStoreError", "TuiPipelineJobStore",
    "pipeline_fingerprint",
]
