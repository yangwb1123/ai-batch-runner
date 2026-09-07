"""Interactive entrypoint for read-only observed native Pipelines.

The TUI may start a pipeline only through an explicit command or option.  The
pipeline remains the execution authority; this mixin validates the local
request, attaches a read-only event projection, exposes only the scoped native
control channel, and renders the final runner outcome.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import os
from pathlib import Path
from typing import Any, Optional

from . import config
from .lock import acquire_lock, release_lock
from .pipeline import load_pipeline, run_pipeline
from .tui_native import (
    NativePipelineControlChannel, NativePipelineAttachment)
from .tui_view_support import clean_text, one_line

_PIPELINE_INPUT_MAX_BYTES = 4096
_PIPELINE_SESSION_MAX_BYTES = 160
_PATH_FIELDS = ("from_dir", "role_dir", "role_keywords", "output_dir")


class TUIPipelineMixin:
    """Run one explicit Pipeline while keeping TUI controls read-only."""

    _pipeline_busy = False
    _pipeline_source = ""
    _pipeline_failed_stages: tuple[str, ...] = ()
    _pipeline_result_count = 0

    def _slash_pipeline(self, args: tuple[str, ...]) -> None:
        if len(args) not in (1, 2) or (len(args) == 2 and args[1] != "--reuse"):
            self._usage("pipeline", "<file> [--reuse]")
            return
        self._start_pipeline_path(args[0], reuse=len(args) == 2)

    def _palette_pipeline(self) -> None:
        self._slash_pipeline(())

    def _start_pipeline_path(self, raw_path: str, *, reuse: bool = False) -> bool:
        if self._busy or self._session_control_busy():
            self.notify("Wait for the active operation before starting a pipeline.",
                        severity="warning")
            return False
        if self._native_pipeline_is_running():
            self.notify("Wait for the attached native pipeline to finish.",
                        severity="warning")
            return False
        try:
            root = _workspace_root(self)
            path = _pipeline_path(raw_path, root)
            pipeline = _load_pipeline(path)
            _prepare_pipeline(pipeline, root, self)
        except (OSError, RuntimeError, SystemExit, TypeError, ValueError) as exc:
            self._append_entry("error", f"Unable to start pipeline: {exc}")
            return False
        if self._native_pipeline_attachment is not None:
            self.detach_native_pipeline()
        try:
            attachment = self.attach_native_pipeline(root)
        except (RuntimeError, TypeError, ValueError) as exc:
            self._append_entry("error", f"Unable to attach pipeline: {exc}")
            return False
        return self._launch_pipeline(pipeline, path, attachment, reuse)

    def _launch_pipeline(self, pipeline: Any, path: Path,
                         attachment: NativePipelineAttachment,
                         reuse: bool) -> bool:
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
                    error = _system_exit_error(
                        exc, "pipeline workspace lock refused")
                else:
                    try:
                        results, failed = run_pipeline(
                            pipeline,
                            model_override=str(
                                getattr(self.options, "model", "") or ""),
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


def _workspace_root(owner: Any) -> Path:
    backend = getattr(owner, "_backend", None)
    value = (getattr(backend, "cwd", "") or
             getattr(getattr(owner, "options", None), "cwd", "") or os.getcwd())
    root = Path(str(value)).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    return root


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
        return load_pipeline(str(path))
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
        config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT = previous
        handlers, propagate, level = previous_logging
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
    code = int(exc.code) if isinstance(exc.code, int) else 1
    return RuntimeError(f"{prefix} (exit {code})")


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


def _display_path(path: Path) -> str:
    text = one_line(clean_text(path))
    encoded = text.encode("utf-8")
    if len(encoded) <= 512:
        return text
    return encoded[:509].decode("utf-8", "ignore") + "..."


__all__ = ["TUIPipelineMixin"]
