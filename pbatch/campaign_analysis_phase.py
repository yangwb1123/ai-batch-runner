"""Analysis-phase admission, execution and terminal budget classification."""

from __future__ import annotations

import time

from . import config
from .campaign_analysis import (analysis_fingerprint, build_analysis_task,
                                parse_directions, run_analysis_tasks)
from .campaign_analysis_retry import ANALYSIS_BUDGET_REASON
from .campaign_budget import bound_task_timeout, context_deadline
from .campaign_models import safe_nonnegative_int
from .campaign_state import failure_fingerprint
from .models import Task
from .text_io import read_text_bounded

def _noop_emit(*_args, **_kwargs) -> None:
    return None


def _reuse_enabled(args) -> bool:
    return not getattr(args, "force", False) and (
        getattr(args, "reuse", False) or getattr(args, "skip_passed", False)
        or getattr(args, "retry_failed", False))


def _analyze_modules(context, modules: list[str],
                     analysis_scope: set[str] | None = None, emit=None):
    emit = emit or _noop_emit
    started = time.monotonic()
    context.analysis_budget_stopped = False
    deadline = context_deadline(context)
    tasks, fingerprints, directions = {}, {}, {}
    failures = 0
    for module in modules:
        if deadline is not None and deadline.expired(time.monotonic()):
            emit(context, module, "__analysis__", "", "BUDGET_BLOCKED", "",
                 [], ANALYSIS_BUDGET_REASON)
            context.analysis_budget_stopped = True
            continue
        task, parsed, fp, deduplicated = _prepare_analysis(
            context, module, analysis_scope, _reuse_enabled(context.args), emit)
        fingerprints[module] = fp
        if parsed:
            directions[module] = parsed
        if deduplicated:
            failures += 1
        if task is not None:
            if deadline is not None and not bound_task_timeout(task, deadline):
                context.analysis_budget_stopped = True
            tasks[module] = task
    output_modules = {str(task.output_path()): module for module, task in tasks.items()}
    results = run_analysis_tasks(
        list(tasks.values()), context.settings.analysis_jobs,
        context.settings.analysis_retries, context.args.retry_delay,
        lambda result: bool(_parse_analysis_file(
            output_modules[str(result.task.output_path())], result.task)),
        deadline=deadline)
    for module, task in tasks.items():
        parsed, failed = _finish_analysis(
            context, module, task, results.get(str(task.output_path())),
            fingerprints[module], emit)
        if parsed:
            directions[module] = parsed
        failures += failed
    context.analysis_elapsed = time.monotonic() - started
    return directions, failures


def _prepare_analysis(context, module: str, analysis_scope, reuse: bool, emit):
    task = build_analysis_task(
        context.root, context.settings, module, context.args.model,
        context.args.provider, context.args.timeout,
        getattr(context.args, "focus", ""))
    fp = analysis_fingerprint(context.root, context.settings, module,
                              context.snapshot, task)
    if reuse and context.store.reusable(module, "__analysis__", fp, ("ANALYZED",)):
        parsed = _parse_analysis_file(module, task)
        if parsed:
            return None, parsed, fp, False
    if analysis_scope is not None and module not in analysis_scope:
        return None, [], fp, False
    previous = context.store.latest_event(module, "__analysis__")
    same_input = previous.get("fingerprint") == fp
    count = safe_nonnegative_int(previous.get("same_failure_count", 0)) if same_input else 0
    if (not getattr(context.args, "force", False) and same_input and count >= 2
            and previous.get("failure_fingerprint")):
        emit(context, module, "__analysis__", "", "ANALYSIS_DEDUPLICATED",
             fp, [str(task.output_path())],
             "unchanged analysis failure repeated twice; change inputs or use --force",
             extra={"failure_fingerprint": previous["failure_fingerprint"],
                    "failure_status": "ANALYSIS_FAILED",
                    "same_failure_count": count})
        return None, [], fp, True
    emit(context, module, "__analysis__", "", "ANALYZING", fp,
         [str(task.output_path())], extra={
             "prior_failure_fingerprint": previous.get(
                 "failure_fingerprint", "") if same_input else "",
             "prior_failure_count": count})
    return task, [], fp, False


def _finish_analysis(context, module: str, task: Task, result,
                     fingerprint: str, emit):
    parsed = _parse_analysis_file(module, task) if result and result.success else []
    if not parsed:
        task.output_path().unlink(missing_ok=True)
        deadline = context_deadline(context)
        if deadline is not None and deadline.expired(time.monotonic()):
            context.analysis_budget_stopped = True
            emit(context, module, "__analysis__", "", "BUDGET_BLOCKED", fingerprint, [],
                 "campaign deadline exhausted during analysis; result is not accepted",
                 extra={"failure_status": "BUDGET_BLOCKED"})
            return [], 0
        reason = result.reason if result else "analysis produced no structured directions"
        signature = failure_fingerprint(fingerprint, "ANALYSIS_FAILED", reason)
        previous = context.store.latest_event(module, "__analysis__")
        prior_signature = previous.get("prior_failure_fingerprint", "")
        prior_count = safe_nonnegative_int(previous.get("prior_failure_count", 0))
        count = prior_count + 1 if prior_signature == signature else 1
        emit(context, module, "__analysis__", "", "ANALYSIS_FAILED",
             fingerprint, [], reason, extra={
                 "failure_fingerprint": signature,
                 "failure_status": "ANALYSIS_FAILED",
                 "same_failure_count": count})
        return [], 1
    emit(context, module, "__analysis__", "", "ANALYZED", fingerprint,
         [str(task.output_path())], elapsed=result.elapsed)
    return parsed, 0


def _parse_analysis_file(module: str, task) -> list:
    path = task.output_path()
    try:
        return parse_directions(module, read_text_bounded(
            path, config.OUTPUT_MAX_BYTES, "campaign analysis"))
    except (OSError, ValueError):
        return []


def _pending_analysis_modules(context, modules: list[str]) -> list[str]:
    if not _reuse_enabled(context.args):
        return list(modules)
    pending = []
    for module in modules:
        task = build_analysis_task(context.root, context.settings, module,
                                   context.args.model, context.args.provider,
                                   context.args.timeout,
                                   getattr(context.args, "focus", ""))
        fingerprint = analysis_fingerprint(context.root, context.settings, module,
                                           context.snapshot, task)
        reusable = context.store.reusable(module, "__analysis__", fingerprint,
                                          ("ANALYZED",))
        if not reusable or not _parse_analysis_file(module, task):
            pending.append(module)
    return pending
