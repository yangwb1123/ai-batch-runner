"""Workspace validation, identity, and runtime helpers for TUI pipelines."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import config
from .tui_pipeline_jobs import (
    PipelineJobBusy, PipelineJobStoreError, TuiPipelineJobStore,
    _pid_alive, pipeline_fingerprint,
)

_PIPELINE_INPUT_MAX_BYTES = 4096
_PIPELINE_SESSION_MAX_BYTES = 160
_PATH_FIELDS = ("from_dir", "role_dir", "role_keywords", "output_dir")


class PipelineWorkspaceLockRefused(RuntimeError):
    """The pipeline runner could not acquire its workspace execution lock."""


def _workspace_root(owner: Any) -> Path:
    backend = getattr(owner, "_backend", None)
    value = (getattr(backend, "cwd", "") or
             getattr(getattr(owner, "options", None), "cwd", "") or os.getcwd())
    root = Path(str(value)).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    return root


def _prepare_pipeline_request(owner: Any, raw_path: str,
                              resume_job: Optional[dict]) -> tuple:
    root = _workspace_root(owner)
    path = _pipeline_path(raw_path, root)
    pipeline = _load_pipeline(path)
    _prepare_pipeline(pipeline, root, owner)
    persistent = bool(getattr(owner.options, "persistent_pipelines", False))
    if persistent and any(bool(getattr(stage, "approval", False))
                          for stage in pipeline.stages):
        raise ValueError(
            "persistent pipelines cannot contain interactive approval stages")
    store, job, fingerprint = _prepare_pipeline_job(
        owner, root, path, resume_job)
    return root, path, pipeline, store, job, fingerprint


def _defer_persistent_job(owner: Any, store: TuiPipelineJobStore,
                          job: dict, owner_is_live: bool) -> bool:
    if not getattr(owner.options, "persistent_pipelines", False):
        return False
    same_starting_process = (
        job["owner_pid"] == os.getpid() and job["owner_id"] == store.owner_id)
    worker_is_live = bool(job["worker_pid"]
                          and _pid_alive(job["worker_pid"]))
    deferred_queue = (job["state"] == "queued"
                      and job["retry_after"] > int(time.time()))
    live_handoff = job["state"] == "handoff_pending" and owner_is_live
    if deferred_queue or live_handoff or (
            job["state"] in {"queued", "running"}
            and (owner_is_live or worker_is_live or same_starting_process)):
        owner._tui_pipeline_job = job
        owner._persistent_notice(
            job["progress"] if not live_handoff
            else "Waiting for pipeline worker to release handoff.")
        return True
    return False


def _pipeline_job_store(owner: Any, root: Path) -> TuiPipelineJobStore:
    current = getattr(owner, "_tui_pipeline_job_store", None)
    if current is not None and current.workspace == root:
        return current
    store = TuiPipelineJobStore(root)
    owner._tui_pipeline_job_store = store
    return store


def _prepare_pipeline_job(owner: Any, root: Path, path: Path,
                          resume_job: Optional[dict]
                          ) -> tuple[Optional[TuiPipelineJobStore], Optional[dict], str]:
    if not owner.options.orchestrator_only:
        return None, None, ""
    store = _pipeline_job_store(owner, root)
    fingerprint = pipeline_fingerprint(path)
    relative = path.relative_to(root).as_posix()
    if resume_job is not None:
        current = store.read()
        if not current or current["job_id"] != resume_job.get("job_id"):
            raise PipelineJobStoreError("pipeline recovery record changed")
        if (resume_job.get("pipeline") != relative
                or resume_job.get("fingerprint") != fingerprint
                or resume_job.get("controller_fingerprint")
                != _controller_fingerprint(owner)):
            raise PipelineJobStoreError("pipeline recovery identity changed")
        if store.owned_by_other_live_process(current):
            raise PipelineJobBusy("an active TUI process owns this pipeline job")
        if current["state"] not in {"queued", "running"}:
            raise PipelineJobStoreError("pipeline job is not resumable")
        return store, current, fingerprint
    if store.active() is not None:
        raise PipelineJobBusy("an unfinished pipeline job must be recovered first")
    return store, None, fingerprint


def _pipeline_execution_settings(owner: Any) -> dict[str, Any]:
    options = getattr(owner, "options", None)
    backend = getattr(owner, "_backend", None)
    memory = str(getattr(options, "memory_mode", "") or config.MEMORY_MODE)
    return {
        "agent": str(getattr(options, "agent", "") or
                     getattr(backend, "agent", "") or config.AGENT_BIN),
        "provider": str(getattr(options, "provider", "") or
                        getattr(backend, "provider", "")),
        "timeout": int(getattr(options, "timeout", 0) or 0),
        "memory_mode": memory if memory in {"auto", "on", "off"} else "auto",
    }


def _controller_fingerprint(owner: Any) -> str:
    options = getattr(owner, "options", None)
    backend = getattr(owner, "_backend", None)
    values = _pipeline_execution_settings(owner)
    values.update({
        "model": str(getattr(options, "model", "") or
                     getattr(backend, "model", "")),
        "session_name": str(getattr(options, "session_name", "tui") or "tui"),
    })
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _persistent_job_identity_matches(owner: Any, root: Path,
                                     job: dict) -> bool:
    try:
        path = _pipeline_path(job["pipeline"], root)
        return (pipeline_fingerprint(path) == job["fingerprint"]
                and job["session_name"]
                == str(owner.options.session_name or "tui")
                and job["controller_fingerprint"]
                == _controller_fingerprint(owner)
                and job["execution"] == _pipeline_execution_settings(owner))
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _spawn_pipeline_worker(root: Path, job_id: str,
                           owner_id: str) -> subprocess.Popen:
    tool_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["PBATCH_DATA_DIR"] = str(config.data_dir(str(root)))
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(tool_root) + (os.pathsep + existing if existing else "")
    return subprocess.Popen(
        [sys.executable, "-m", "pbatch.tui_pipeline_worker",
         "--workspace", str(root), "--job-id", job_id, "--owner-id", owner_id],
        cwd=str(root), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True)


def _pipeline_path(raw_path: str, root: Path) -> Path:
    value = str(raw_path or "")
    if not value or "\x00" in value:
        raise ValueError("pipeline path must not be empty or contain NUL")
    if len(value.encode("utf-8")) > _PIPELINE_INPUT_MAX_BYTES:
        raise ValueError("pipeline request is too long")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    path = candidate.resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"pipeline file is not a regular file: {path}")
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError("pipeline file must use .yaml or .yml")
    _inside_workspace(path, root, "pipeline file")
    return path


def _load_pipeline(path: Path) -> Any:
    try:
        from . import tui_pipeline
        return tui_pipeline.load_pipeline(str(path))
    except SystemExit as exc:
        raise ValueError("pipeline definition is invalid") from exc


def _prepare_pipeline(pipeline: Any, root: Path, owner: Any) -> None:
    agent = str(getattr(owner, "_option_value", lambda _name: "")("agent") or
                config.AGENT_BIN)
    options = getattr(owner, "options", None)
    provider = str(getattr(options, "provider", "") or "")
    for stage in pipeline.stages:
        stage.cwd = _workspace_value(stage.cwd or str(root), root, "stage cwd")
        if not stage.agent:
            stage.agent = agent
        if not stage.provider and provider:
            stage.provider = provider
        for field in _PATH_FIELDS:
            value = getattr(stage, field, "")
            if value:
                setattr(stage, field, _workspace_value(value, root, field))
        _prepare_task_scopes(stage, root)
    if pipeline.decision_log:
        pipeline.decision_log = _workspace_value(
            pipeline.decision_log, root, "decision log")
    if pipeline.archive_dir:
        pipeline.archive_dir = _workspace_value(
            pipeline.archive_dir, root, "archive directory")


def _prepare_task_scopes(stage: Any, root: Path) -> None:
    for task in stage.tasks if isinstance(stage.tasks, list) else ():
        if not isinstance(task, dict):
            continue
        if task.get("cwd"):
            task["cwd"] = _workspace_value(task["cwd"], root, "task cwd")
        output = task.get("output", "")
        if output:
            base = Path(task.get("cwd") or stage.cwd)
            _workspace_value(output, base, "task output")
    if stage.output:
        _workspace_value(stage.output, Path(stage.cwd), "stage output")


def _workspace_value(value: Any, root: Path, label: str) -> str:
    text = str(value or "")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    _inside_workspace(resolved, root, label)
    return str(resolved)


def _inside_workspace(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} is outside the TUI workspace") from exc


@contextmanager
def _pipeline_runtime_config(owner: Any):
    previous = (config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT)
    previous_logging = (list(config.log.handlers), config.log.propagate,
                        config.log.level)
    agent = str(getattr(owner, "_option_value", lambda _name: "")("agent") or
                previous[0])
    memory = str(getattr(owner, "_option_value", lambda _name: "")(
        "memory_mode") or previous[1]).lower()
    handler = _PipelineLogHandler(owner)
    config.AGENT_BIN = agent
    config.MEMORY_MODE = memory if memory in {"auto", "on", "off"} else previous[1]
    config.STREAM_OUTPUT = "none"
    config.log.handlers = [handler]
    config.log.propagate = False
    config.log.setLevel(logging.INFO)
    try:
        yield
    finally:
        handlers, propagate, level = previous_logging
        config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT = previous
        config.log.handlers, config.log.propagate = handlers, propagate
        config.log.setLevel(level)


class _PipelineLogHandler(logging.Handler):
    def __init__(self, owner: Any) -> None:
        super().__init__()
        self._owner = owner
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._owner._emit_from_backend("status", self.format(record))
        except Exception:
            pass


def _system_exit_error(exc: SystemExit, prefix: str) -> RuntimeError:
    return RuntimeError(f"{prefix} (exit {_exit_code(exc)})")


def _exit_code(exc: SystemExit) -> int:
    return int(exc.code) if isinstance(exc.code, int) else 1


def _pipeline_cwd(pipeline: Any) -> str:
    stages = getattr(pipeline, "stages", ())
    if stages:
        return str(getattr(stages[0], "cwd", "") or os.getcwd())
    return os.getcwd()


def _pipeline_session_name(owner: Any) -> str:
    base = str(getattr(getattr(owner, "options", None), "session_name", "tui") or "tui")
    value = f"{base}-pipeline"
    encoded = value.encode("utf-8")
    if len(encoded) <= _PIPELINE_SESSION_MAX_BYTES:
        return value
    return encoded[:_PIPELINE_SESSION_MAX_BYTES - 3].decode("utf-8", "ignore") + "..."
