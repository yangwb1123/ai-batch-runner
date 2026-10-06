"""Trace identity and event emission helpers for task execution."""

from __future__ import annotations

from typing import Optional

from . import config, metering
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
                   workdir: str, agent_bin: str = "") -> TaskResult:
    """Attach session metadata without changing the task outcome."""
    result.session_id = session_id
    result.session_name = session_name
    from .memory import memory_enabled
    if session_id and memory_enabled(agent_bin):
        path = session_file(workdir, session_id)
        result.raw_session = str(path) if path else ""
    return result


def finish_task_result(result: TaskResult, task: Task, session_id: str,
                       session_name: str, workdir: str,
                       usage_before: Optional[dict], task_id: str,
                       attempt: int) -> TaskResult:
    """Finalize a task result and emit its execution event."""
    attach_session(result, session_id, session_name, workdir,
                   task.effective_agent())
    annotate_result(result, task_id, str(task.memory.get("stage", "")), attempt)
    result.usage = metering.record_event(
        "task_finish" if result.success else "task_fail", task.prompt,
        result.reason, workdir, session_id or session_name, usage_before,
        run_id=result.run_id, task_id=result.task_id,
        stage_id=result.stage_id, attempt=result.attempt, phase="execution",
        module=str(task.memory.get("module", "")), **_parent_fields(task))
    if config.AGENT_LEDGER_ENABLED:
        from .agent_ledger import record_result
        record_result(result, task, workdir, phase="execution")
    _attempt_fact("completed", result, task, workdir, "execution")
    return result


def record_validation_event(result: TaskResult, task: Task) -> None:
    """Record a validator result when a validator actually ran."""
    if result.validation_ok is None:
        return
    metering.record_event(
        "task_validation_pass" if result.success else "task_validation_fail",
        task.prompt, result.reason, task.workdir(), result.session_name,
        task_id=result.task_id, stage_id=result.stage_id,
        attempt=result.attempt, phase="validation",
        module=str(task.memory.get("module", "")), **_parent_fields(task))
    if config.AGENT_LEDGER_ENABLED:
        from .agent_ledger import record_result
        record_result(result, task, task.workdir(), phase="validation")
    _attempt_fact("completed", result, task, task.workdir(), "validation")


def _parent_fields(task):
    from .task_lifecycle import parent_fields
    return parent_fields(task)


def record_task_started(task, task_id, attempt, workdir, session_id, session_name, pid):
    _attempt_fact("started", task, task_id, attempt, workdir, session_id, session_name, pid)


def bind_parallel_tasks(tasks):
    from .task_lifecycle import bind_tasks
    try:
        bind_tasks(tasks)
    except Exception as exc:
        config.log.warning("Task lineage unavailable: %s", type(exc).__name__)


def flush_attempt_facts():
    from .task_attempt_facts import flush
    try:
        flush()
    except Exception as exc:
        config.log.warning("Task observation drain lost: %s", type(exc).__name__)


def _attempt_fact(method, *args):
    from . import task_attempt_facts
    try:
        getattr(task_attempt_facts, method)(*args)
    except Exception as exc:
        config.log.warning("Task attempt observation lost: %s", type(exc).__name__)
