"""Trace identity and event emission helpers for task execution."""

from __future__ import annotations

from typing import Optional

from . import metering
from .models import Task, TaskResult
from .session import session_file


def annotate_result(result: TaskResult, task_id: str, stage_id: str,
                    attempt: int) -> TaskResult:
    """Attach the current execution identity to a result."""
    result.run_id = metering.RUN_ID
    result.task_id = task_id
    result.stage_id = stage_id
    result.attempt = attempt
    return result


def attach_session(result: TaskResult, session_id: str, session_name: str,
                   workdir: str) -> TaskResult:
    """Attach session metadata without changing the task outcome."""
    result.session_id = session_id
    result.session_name = session_name
    if session_id:
        path = session_file(workdir, session_id)
        result.raw_session = str(path) if path else ""
    return result


def finish_task_result(result: TaskResult, task: Task, session_id: str,
                       session_name: str, workdir: str,
                       usage_before: Optional[dict], task_id: str,
                       attempt: int) -> TaskResult:
    """Finalize a task result and emit its execution event."""
    attach_session(result, session_id, session_name, workdir)
    annotate_result(result, task_id, str(task.memory.get("stage", "")), attempt)
    metering.record_event(
        "task_finish" if result.success else "task_fail", task.prompt,
        result.reason, workdir, session_id or session_name, usage_before,
        run_id=result.run_id, task_id=result.task_id,
        stage_id=result.stage_id, attempt=result.attempt, phase="execution")
    return result


def record_validation_event(result: TaskResult, task: Task) -> None:
    """Record a validator result when a validator actually ran."""
    if result.validation_ok is None:
        return
    metering.record_event(
        "task_validation_pass" if result.success else "task_validation_fail",
        task.prompt, result.reason, task.workdir(), result.session_name,
        task_id=result.task_id, stage_id=result.stage_id,
        attempt=result.attempt, phase="validation")
