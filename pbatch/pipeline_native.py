"""Optional native pipeline observation helpers.

This module keeps the ordinary pipeline implementation small and unchanged
when no event sink is supplied.  All callbacks are injected to avoid a
pipeline/native-adapter import cycle.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

from .execution_trace import flush_attempt_facts
from .task_lifecycle import close_scope, notify, open_scope
from .tui_native import (
    NativePipelineApprovalChannel, NativePipelineObserver,
    close_native_observation, open_native_observation,
)


RunOr = Callable[..., tuple[list, list]]
RunParallel = Callable[..., list]
RunSerial = Callable[..., list]


def execute_stage_tasks(stage: Any, tasks: list, reused_outputs: list,
                        stage_validate: str, session_mode: str,
                        session_name: str, stage_session_id: str,
                        retries: int, retry_delay: float, backoff: float,
                        session_state: Optional[set], observer: Optional[NativePipelineObserver],
                        run_or: RunOr, run_parallel: RunParallel,
                        run_serial: RunSerial) -> tuple[list, list]:
    """Execute one stage and optionally publish task/validator facts."""
    cancel_event = _cancel_event(observer)
    task_ids = _start_tasks(observer, stage, tasks, stage_validate)
    _register_task_controls(observer, task_ids, tasks)
    result_callback = _task_control_callback(observer, task_ids)
    try:
        if stage.or_tasks:
            run_kwargs = {"cancel_event": cancel_event}
            if result_callback is not None:
                run_kwargs["result_callback"] = result_callback
            results, outputs = run_or(
                stage, tasks, reused_outputs, stage_validate, session_mode,
                session_name, stage_session_id, session_state, **run_kwargs)
        elif stage.mode == "parallel":
            kwargs = {"validate_cmd": stage_validate, "retries": retries,
                      "retry_delay": retry_delay, "backoff": backoff}
            if cancel_event is not None:
                kwargs["cancel_event"] = cancel_event
            if result_callback is not None:
                kwargs["result_callback"] = result_callback
            results = run_parallel(tasks, stage.workers, **kwargs)
            outputs = _outputs(reused_outputs, results)
        else:
            kwargs = {
                "retries": retries, "retry_delay": retry_delay,
                "backoff": backoff, "session_mode": session_mode,
                "session_id": stage_session_id, "session_name": session_name,
                "validate_cmd": stage_validate, "session_state": session_state,
                "cancel_event": cancel_event,
            }
            if result_callback is not None:
                kwargs["result_callback"] = result_callback
            results = run_serial(tasks, **kwargs)
            outputs = _outputs(reused_outputs, results)
    finally:
        _unregister_task_controls(observer, task_ids, tasks)
    _finish_tasks(observer, stage, stage_validate, task_ids, results)
    return results, outputs


def _task_control_callback(observer: Optional[NativePipelineObserver],
                            task_ids: dict[int, str]):
    channel = observer.approval_channel if observer is not None else None
    unregister = getattr(channel, "unregister_task", None)
    if not callable(unregister):
        return None

    def release(result: Any) -> None:
        task = getattr(result, "task", None)
        unregister(task_ids.get(id(task), ""), task)

    return release


def _register_task_controls(observer: Optional[NativePipelineObserver],
                             task_ids: dict[int, str], tasks: list) -> None:
    channel = observer.approval_channel if observer is not None else None
    register = getattr(channel, "register_task", None)
    if not callable(register):
        return
    for task in tasks:
        register(task_ids.get(id(task), ""), task)


def _unregister_task_controls(observer: Optional[NativePipelineObserver],
                               task_ids: dict[int, str], tasks: list) -> None:
    channel = observer.approval_channel if observer is not None else None
    unregister = getattr(channel, "unregister_task", None)
    if not callable(unregister):
        return
    for task in tasks:
        unregister(task_ids.get(id(task), ""), task)


def _start_tasks(observer: Optional[NativePipelineObserver], stage: Any,
                 tasks: list, validator: str) -> dict[int, str]:
    if observer is None:
        return {}
    task_ids = {}
    for index, task in enumerate(tasks, 1):
        item_id = observer.task_started(stage.name, index, len(tasks), task)
        task_ids[id(task)] = item_id
        if validator or task.validate is not None:
            observer.validation_started(item_id, task, stage=stage.name)
    return task_ids


def _finish_tasks(observer: Optional[NativePipelineObserver], stage: Any,
                  validator: str, task_ids: dict[int, str], results: list) -> None:
    if observer is None:
        return
    for index, result in enumerate(results, 1):
        item_id = task_ids.get(id(result.task),
                               observer.task_id(stage.name, index, result.task))
        observer.task_completed(item_id, result.task, result, stage=stage.name)
        if validator or result.task.validate is not None:
            observer.validation_completed(
                item_id, result.task, result, stage=stage.name)


def _outputs(reused_outputs: list, results: list) -> list:
    outputs = list(reused_outputs)
    for result in results:
        if result.success and result.task.output:
            outputs.append(str(result.task.output_path()))
    return outputs


def run_pipeline_observed(pipeline: Any, model_override: str = "",
                          dry_run: bool = False, reuse: bool = False,
                          timeout_override: int = 0, session_mode: str = "new",
                          session_name: str = "", validate_cmd: str = "",
                          decision_log: str = "", archive_dir: str = "",
                          reuse_legacy: bool = False,
                          fingerprint_mode: bool = False,
                          approval_file: str = "", original_prompt: str = "",
                          retries: int = 0, retry_delay: float = 10.0,
                          backoff: float = 2.0, event_sink=None,
                          approval_channel: Optional[NativePipelineApprovalChannel] = None):
    """Run the existing pipeline with an opt-in native observation sink."""
    from . import pipeline as implementation

    all_results: list = []
    failed_stages: list[str] = []
    stage_outputs: dict[str, list[str]] = {}
    session_state: set = set()
    cwd = (pipeline.stages[0].cwd if pipeline.stages and
           pipeline.stages[0].cwd else os.getcwd())
    observer, token, observed = open_native_observation(
        event_sink, cwd, dry_run, approval_channel)
    finished = False
    cancelled = False
    facts, fact_token = open_scope(pipeline, cwd, dry_run)
    try:
        _log_and_run_stages(
            implementation, pipeline, dry_run, reuse, observer, observed,
            stage_outputs, failed_stages, all_results, session_state,
            model_override, timeout_override, session_mode, session_name,
            validate_cmd, reuse_legacy, fingerprint_mode, approval_file,
            original_prompt, decision_log, retries, retry_delay, backoff, facts)
        cancelled = _control_cancelled(observer)
        if not failed_stages and not cancelled and (pipeline.archive_dir or archive_dir):
            implementation._archive_pipeline_results(
                pipeline, all_results, archive_dir, session_name)
        finished = True
        return all_results, failed_stages
    finally:
        cancelled = cancelled or _control_cancelled(observer)
        if facts:
            flush_attempt_facts()
        close_scope(facts, fact_token, finished, failed_stages, cancelled)
        close_native_observation(
            observer, token, observed, finished, failed_stages, cancelled)


def _log_and_run_stages(implementation: Any, pipeline: Any, dry_run: bool,
                        reuse: bool, observer: Optional[NativePipelineObserver],
                        observed: bool, stage_outputs: dict[str, list],
                        failed_stages: list[str], all_results: list,
                        session_state: set, model_override: str,
                        timeout_override: int, session_mode: str,
                        session_name: str, validate_cmd: str,
                        reuse_legacy: bool, fingerprint_mode: bool,
                        approval_file: str, original_prompt: str,
                        decision_log: str, retries: int, retry_delay: float,
                        backoff: float, facts=None) -> None:
    implementation._log_pipeline_start(pipeline, reuse)
    if observed:
        observer.start(pipeline.name, len(pipeline.stages))
        observer.plan_updated(deterministic_preflight_plan(
            pipeline, validate_cmd=validate_cmd, retries=retries,
            timeout_override=timeout_override))
    for index, stage in enumerate(pipeline.stages, 1):
        if _control_cancelled(observer):
            break
        if dry_run:
            from .from_dir import _describe_stage
            _describe_stage(stage, reuse)
            continue
        _stage_started(facts, observer, stage.name, index, len(pipeline.stages))
        stop, results = implementation._run_pipeline_stage(
            stage, stage_outputs, failed_stages, model_override, reuse,
            timeout_override, session_mode, session_name, validate_cmd,
            reuse_legacy, fingerprint_mode, approval_file, original_prompt,
            decision_log or pipeline.decision_log, retries, retry_delay,
            backoff, session_state)
        all_results.extend(results)
        cancelled = _control_cancelled(observer)
        _stage_completed(facts, observer, stage.name, index, len(pipeline.stages),
                         not stop and not cancelled, results, cancelled)
        if stop or cancelled:
            break


def _stage_started(facts, observer, name: str, index: int, total: int) -> None:
    notify(facts, "stage_started", name)
    if observer:
        observer.stage_started(name, index, total)


def _stage_completed(facts, observer, name: str, index: int, total: int,
                     success: bool, results: list, cancelled: bool) -> None:
    notify(facts, "stage_completed", name, success, cancelled)
    if observer:
        successful_tasks = sum(result.success for result in results)
        observer.stage_completed(name, index, total, success, successful_tasks,
                                 cancelled=cancelled)
        observer.pipeline_checkpoint(name, index, total, success,
            task_count=len(results), successful_tasks=successful_tasks, cancelled=cancelled)


def _cancel_event(observer: Optional[NativePipelineObserver]):
    channel = observer.approval_channel if observer is not None else None
    return getattr(channel, "cancel_router", None) or getattr(
        channel, "cancel_event", None)


def _control_cancelled(observer: Optional[NativePipelineObserver]) -> bool:
    channel = observer.approval_channel if observer is not None else None
    return bool(getattr(channel, "cancelled", False))


_PREFLIGHT_LIST_LIMIT = 32
_PREFLIGHT_VALUE_BYTES = 512
_PREFLIGHT_ID_BYTES = 256


class _PreflightList(list):
    """Bounded plan collection that records dropped explicit values."""

    truncated = False


def deterministic_preflight_plan(pipeline: Any, *, validate_cmd: str = "",
                                  retries: int = 0,
                                  timeout_override: int = 0) -> dict[str, Any]:
    """Describe only explicit Pipeline configuration as a display plan."""
    stages = getattr(pipeline, "stages", ())
    stages = stages if isinstance(stages, (list, tuple)) else ()
    root = _preflight_root(stages)
    reads = _PreflightList()
    writes = _PreflightList()
    commands = _PreflightList()
    validators = _PreflightList()
    gates = _PreflightList()
    steps = []
    for index, stage in enumerate(stages[:64], 1):
        name = _preflight_text(getattr(stage, "name", ""), _PREFLIGHT_ID_BYTES)
        name = name or f"stage-{index}"
        base = str(getattr(stage, "cwd", "") or root)
        _preflight_stage_paths(stage, base, root, reads, writes)
        _preflight_stage_commands(stage, commands)
        effective = (getattr(stage, "validate_cmd", None)
                     if getattr(stage, "validate_cmd", None) is not None
                     else validate_cmd)
        _preflight_add(validators, effective, f"{name}: ")
        _preflight_task_metadata(stage, base, root, writes, validators)
        if getattr(stage, "gate", False):
            _preflight_add(gates, name, "gate:")
        if getattr(stage, "approval", False):
            _preflight_add(gates, name, "approval:")
        steps.append(_preflight_step(stage, name, index))
    _preflight_path_add(
        writes, getattr(pipeline, "decision_log", ""), root, root)
    _preflight_path_add(
        writes, getattr(pipeline, "archive_dir", ""), root, root)
    budget = _preflight_budget(stages, retries, timeout_override)
    pipeline_name = _preflight_text(getattr(pipeline, "name", "pipeline"),
                                    _PREFLIGHT_VALUE_BYTES) or "pipeline"
    return {
        "item_id": f"pipeline:{pipeline_name}",
        "goal": f"Execute pipeline: {pipeline_name}",
        "source": "deterministic-preflight", "version": "1",
        "read_scope": reads, "write_scope": writes,
        "commands": commands, "validators": validators, "gates": gates,
        "budget": budget, "items": steps,
        "truncated": len(stages) > 64 or any(
            _preflight_sequence_too_long(getattr(stage, "from_outputs", ""))
            for stage in stages[:64]),
        "boundaries_truncated": any(
            getattr(values, "truncated", False)
            for values in (reads, writes, commands, validators, gates)),
    }


def _preflight_root(stages: tuple | list) -> str:
    first = stages[0] if stages else None
    return str(getattr(first, "cwd", "") or os.getcwd())


def _preflight_stage_paths(stage: Any, base: str, root: str,
                           reads: list, writes: list) -> None:
    _preflight_path_add(reads, getattr(stage, "from_dir", ""), base, root)
    _preflight_path_add(writes, getattr(stage, "output", ""), base, root)
    _preflight_path_add(writes, getattr(stage, "output_dir", ""), base, root)
    source = getattr(stage, "from_outputs", "")
    for value in _preflight_values(source):
        _preflight_add(reads, value, "stage:")


def _preflight_stage_commands(stage: Any, commands: list) -> None:
    for name in ("commands", "verify_experiment"):
        values = getattr(stage, name, ())
        for value in _preflight_values(values):
            _preflight_add(commands, value, "")


def _preflight_task_metadata(stage: Any, base: str, root: str,
                             writes: list, validators: list) -> None:
    tasks = getattr(stage, "tasks", ())
    if not isinstance(tasks, (list, tuple)):
        return
    for task in tasks:
        if isinstance(task, dict):
            task_base = str(task.get("cwd") or base)
            _preflight_path_add(writes, task.get("output", ""), task_base, root)
            _preflight_add(validators, task.get("validate",
                                                  task.get("validate_cmd", "")),
                           f"{getattr(stage, 'name', 'stage')}: ")
        else:
            _preflight_path_add(writes, getattr(task, "output", ""), base, root)
            _preflight_add(validators, getattr(task, "validate", ""),
                           f"{getattr(stage, 'name', 'stage')}: ")


def _preflight_step(stage: Any, name: str, index: int) -> dict[str, Any]:
    source = getattr(stage, "from_outputs", "")
    dependencies = _preflight_values(source)
    return {
        "id": name, "title": name, "state": "pending",
        "actor": _preflight_text(getattr(stage, "agent", ""),
                                  _PREFLIGHT_VALUE_BYTES),
        "dependencies": list(dependencies), "index": index,
    }


def _preflight_budget(stages: tuple | list, retries: int,
                      timeout_override: int) -> dict[str, str]:
    values: dict[str, str] = {}
    retry_count = _preflight_nonnegative(retries)
    if retry_count:
        values["retries"] = str(retry_count)
    task_timeout = _preflight_nonnegative(timeout_override)
    if task_timeout:
        values["task_timeout_s"] = str(task_timeout)
    timeout = max((
        _preflight_nonnegative(getattr(stage, "command_timeout", 0))
        for stage in stages), default=0)
    if timeout:
        values["command_timeout_s"] = str(timeout)
    return values


def _preflight_nonnegative(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _preflight_path_add(target: list, value: Any, base: str, root: str) -> None:
    path = _preflight_relative_path(value, base, root)
    if path:
        _preflight_add(target, path, "")


def _preflight_relative_path(value: Any, base: str, root: str) -> str:
    if not value or not isinstance(value, (str, os.PathLike)):
        return ""
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(base) / candidate
    try:
        relative = candidate.resolve(strict=False).relative_to(
            Path(root).resolve(strict=False))
    except (OSError, ValueError):
        return ""
    return _preflight_text(relative.as_posix(), _PREFLIGHT_VALUE_BYTES)


def _preflight_add(target: list, value: Any, prefix: str) -> None:
    text = _preflight_text(value, _PREFLIGHT_VALUE_BYTES)
    if not text:
        return
    rendered = prefix + text
    if rendered in target:
        return
    if len(target) >= _PREFLIGHT_LIST_LIMIT:
        if isinstance(target, _PreflightList):
            target.truncated = True
        return
    target.append(rendered)


def _preflight_values(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (str, os.PathLike)):
        return (value,) if str(value) else ()
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value[:_PREFLIGHT_LIST_LIMIT]
                     if item is not None and str(item))
    return ()


def _preflight_sequence_too_long(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) > _PREFLIGHT_LIST_LIMIT


def _preflight_text(value: Any, maximum: int) -> str:
    text = "" if value is None else " ".join(str(value).split())
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    return encoded[:maximum - 3].decode("utf-8", "ignore") + "..."


__all__ = ["deterministic_preflight_plan", "execute_stage_tasks", "run_pipeline_observed"]
