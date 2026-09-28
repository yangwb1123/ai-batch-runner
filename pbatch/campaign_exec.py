from __future__ import annotations

"""Campaign execution layer (split from campaign.py for line budget).

模块分析、方向选择、实现调度、事件记录与轮循环——被 `pi-batch campaign`
主入口 (pbatch/campaign.py) 调用。
"""

# Repository-level campaign coordinator: discover, analyze, select, implement.
from pbatch.pipeline_status import PipelineStatus
import argparse
import time
from pathlib import Path

from . import config, metering
from .campaign_analysis import (analysis_fingerprint, build_analysis_task,
                                discover_modules, parse_directions,
                                run_analysis_tasks, select_directions)
from .campaign_models import CampaignSettings, Direction
from .campaign_merge import record_campaign_outcomes
from .campaign_metrics import build_efficiency_metrics, execution_attempts
from .campaign_preflight import (_preflight_collect_specs,
                                 _preflight_run_one, _preflight_validators,
                                 _provider_breaker_admission,
                                 _report_working_tree_residue,
                                 _start_heartbeat)
from .campaign_pipeline import (PipelineOutcome, direction_fingerprint,
                                parallel_ready, run_direction,
                                run_directions_isolated)
from .campaign_state import (StateStore, _git_output, digest, failure_fingerprint,
                             git_snapshot, git_worktree_evidence, tool_digest, tool_head,
                             write_summary)
from .campaign_strategy import (no_pending_plan, plan_modules,
                                read_module_signals, read_usage)
from .models import Task
from .config import log
from .lock import acquire_lock, release_lock
from .registry import mark_running, unregister
from .runner import kill_active_procs, run_argv
from .text_io import read_text_bounded
from .campaign_recovery import update_round_recovery


class CampaignSignalInterrupt(KeyboardInterrupt):
    """Coordinator-owned SIGTERM, distinct from an operator Ctrl-C."""


def _record_evolution_trajectory(context: "CampaignContext", outcomes: list) -> None:
    """批次完成事件 → 用户级 evolution/state.jsonl（演化轨迹数据源）。

    每个方向一次追加事件 {event: campaign_done, module, direction,
    status}——后续 `evolution report`/轨迹分析按此重建项目演化路径
    （跨批次的"为什么走这些路线"证据链）。追加式、单条有界。
    """
    from .evolution import _append_state
    for outcome in outcomes:
        try:
            _append_state(context.root, {
                "event": "campaign_done",
                "module": getattr(outcome, "module", "") or "",
                "direction": str(getattr(outcome, "direction", "") or "")[:120],
                "status": str(getattr(outcome, "status", "")),
            })
        except OSError:
            log.warning("evolution trajectory: cannot append state event")


def run_campaign(context: CampaignContext) -> int:
    from .campaign import CampaignContext  # noqa: F401（类型仅注解，运行时无害）
    _start_heartbeat()
    modules = discover_modules(context.root, context.settings, context.args.modules)
    if not modules:
        log.error("CAMPAIGN: no modules found")
        return 1
    plan = _campaign_plan(context, modules)
    if context.args.dry_run:
        _print_plan(context, list(plan.selected), plan)
        return 0
    if plan.blocked:
        return _blocked_campaign(context, plan)
    analysis_scope = set(plan.selected)
    pause_reason, pause_evidence = _provider_breaker_admission(
        getattr(context.args, "provider", ""),
        getattr(context.args, "agent_bin", ""))
    if pause_reason:
        _event(context, "__provider__", "__provider__", "",
               PipelineStatus.PROVIDER_BREAKER_PAUSED, "", pause_evidence,
               reason=pause_reason)
        write_summary(context.store,
                      context.settings.summary_path(context.root),
                      context.settings.name)
        log.error("CAMPAIGN paused before analysis: %s", pause_reason)
        return 1
    _preflight_validators(context)
    _event(context, "__campaign__", "__plan__", "", "CAMPAIGN_PLAN", "",
           [], reason=plan.reason, extra={"strategy": plan.mapping()})
    _event(context, "__tool__", "__tool__", "", "TOOL_VERSION",
           tool_digest(), [tool_head()],
           reason="tool code digest (pbatch + pi-batch.py + pi-batch.yaml); "
                  "invalidates analysis/direction reuse when the tool changes")
    directions, failures = _analyze_modules(context, modules, analysis_scope)
    selected, selection_failures = _select(context, modules, directions)
    failures += selection_failures
    outcomes = _implement(context, selected)
    failures += sum(1 for outcome in outcomes if outcome.status != PipelineStatus.PASSED)
    _record_evolution_trajectory(context, outcomes)
    _report_working_tree_residue(context)
    write_summary(context.store, context.settings.summary_path(context.root),
                  context.settings.name)
    passed = sum(1 for outcome in outcomes if outcome.status == PipelineStatus.PASSED)
    log.info("CAMPAIGN complete: %d passed, %d failed", passed, failures)
    return 1 if failures else 0

def _blocked_campaign(context: CampaignContext, plan) -> int:
    _event(context, "__campaign__", "__plan__", "", "CAMPAIGN_PLAN", "",
           [], reason=plan.reason, extra={"strategy": plan.mapping()})
    log.error("CAMPAIGN blocked by module strategy: %s", plan.reason)
    write_summary(context.store,
                  context.settings.summary_path(context.root),
                  context.settings.name)
    return 3


def _analyze_modules(context: CampaignContext, modules: list[str],
                     analysis_scope: set[str] | None = None) -> tuple[dict[str, list[Direction]], int]:
    started = time.monotonic()
    tasks, fingerprints, directions = {}, {}, {}
    failures = 0
    for module in modules:
        task, parsed, fp, deduplicated = _prepare_analysis(
            context, module, analysis_scope, _reuse_enabled(context.args))
        fingerprints[module] = fp
        if parsed:
            directions[module] = parsed
        if deduplicated:
            failures += 1
        if task is not None:
            tasks[module] = task
    output_modules = {str(task.output_path()): module for module, task in tasks.items()}
    results = run_analysis_tasks(
        list(tasks.values()), context.settings.analysis_jobs,
        context.settings.analysis_retries, context.args.retry_delay,
        lambda result: bool(_parse_analysis_file(
            output_modules[str(result.task.output_path())], result.task)))
    for module, task in tasks.items():
        parsed, failed = _finish_analysis(
            context, module, task, results.get(str(task.output_path())),
            fingerprints[module])
        if parsed:
            directions[module] = parsed
        failures += failed
    context.analysis_elapsed = time.monotonic() - started
    return directions, failures


def _prepare_analysis(context, module: str, analysis_scope,
                      reuse: bool) -> tuple[Task | None, list[Direction], str, bool]:
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
    count = _safe_nonnegative_int(previous.get("same_failure_count", 0)) \
        if same_input else 0
    if (not getattr(context.args, "force", False) and same_input and count >= 2
            and previous.get("failure_fingerprint")):
        _event(context, module, "__analysis__", "", "ANALYSIS_DEDUPLICATED",
               fp, [str(task.output_path())],
               "unchanged analysis failure repeated twice; change inputs or use --force",
               extra={"failure_fingerprint": previous["failure_fingerprint"],
                      "failure_status": "ANALYSIS_FAILED",
                      "same_failure_count": count})
        return None, [], fp, True
    _event(context, module, "__analysis__", "", "ANALYZING", fp,
           [str(task.output_path())], extra={
               "prior_failure_fingerprint": previous.get(
                   "failure_fingerprint", "") if same_input else "",
               "prior_failure_count": count})
    return task, [], fp, False


def _finish_analysis(context, module: str, task: Task, result,
                     fingerprint: str) -> tuple[list[Direction], int]:
    parsed = _parse_analysis_file(module, task) if result and result.success else []
    if not parsed:
        task.output_path().unlink(missing_ok=True)
        reason = result.reason if result else "analysis produced no structured directions"
        signature = failure_fingerprint(fingerprint, "ANALYSIS_FAILED", reason)
        previous = context.store.latest_event(module, "__analysis__")
        prior_signature = previous.get("prior_failure_fingerprint", "")
        prior_count = _safe_nonnegative_int(previous.get("prior_failure_count", 0))
        count = prior_count + 1 if prior_signature == signature else 1
        _event(context, module, "__analysis__", "", "ANALYSIS_FAILED",
               fingerprint, [], reason, extra={
                   "failure_fingerprint": signature,
                   "failure_status": "ANALYSIS_FAILED",
                   "same_failure_count": count})
        return [], 1
    _event(context, module, "__analysis__", "", "ANALYZED", fingerprint,
           [str(task.output_path())], elapsed=result.elapsed)
    return parsed, 0

def _parse_analysis_file(module: str, task) -> list[Direction]:
    path = task.output_path()
    try:
        return parse_directions(module, read_text_bounded(
            path, config.OUTPUT_MAX_BYTES, "campaign analysis"))
    except (OSError, ValueError):
        return []

def _select(context: CampaignContext, modules: list[str],
            directions: dict[str, list[Direction]]) -> tuple[list[tuple[Direction, Path, str]], int]:
    selected_items = []
    failures = 0
    for module in modules:
        candidates = directions.get(module, [])
        remaining = context.settings.max_total_directions - len(selected_items)
        if context.settings.max_total_directions and remaining <= 0:
            for direction in candidates:
                _direction_event(
                    context, direction, "DIRECTION_REJECTED", "", _analysis_path(context, module),
                    "global selection quota exhausted")
            continue
        limit = context.settings.max_directions
        if context.settings.max_total_directions:
            limit = min(limit, remaining)
        selected, rejected = select_directions(
            context.root, candidates, limit, context.settings.minimum_score)
        analysis = _analysis_path(context, module)
        for direction, reason in rejected:
            _direction_event(context, direction, "DIRECTION_REJECTED", "", analysis, reason)
        if candidates and not selected:
            failures += 1
        for direction in selected:
            item, blocked = _prepare_direction(context, direction, analysis)
            if item:
                selected_items.append(item)
            failures += int(blocked)
    return selected_items, failures


def _prepare_direction(context, direction: Direction, analysis: Path):
    fingerprint = direction_fingerprint(
        context.root, context.settings, direction, analysis, context.snapshot,
        context.args.model, context.args.provider)
    if (_reuse_enabled(context.args) and context.store.reusable(
            direction.module, direction.direction_id, fingerprint,
            (PipelineStatus.PASSED,))):
        return None, False
    previous = context.store.latest_event(direction.module, direction.direction_id)
    same_input = previous.get("fingerprint") == fingerprint
    count = (_safe_nonnegative_int(previous.get("same_failure_count", 0))
             if same_input else 0)
    if (not getattr(context.args, "force", False) and same_input and count >= 2
            and previous.get("failure_fingerprint")):
        _direction_event(
            context, direction, "DIRECTION_DEDUPLICATED", fingerprint, analysis,
            "unchanged failure repeated twice; change inputs or use --force",
            extra={"failure_fingerprint": previous["failure_fingerprint"],
                   "failure_status": previous.get("failure_status", ""),
                   "same_failure_count": count})
        return None, True
    _direction_event(
        context, direction, "SELECTED", fingerprint, analysis,
        extra={"prior_failure_fingerprint": previous.get(
            "failure_fingerprint", "") if same_input else "",
            "prior_failure_count": count})
    return (direction, analysis, fingerprint), False

def _analysis_path(context: CampaignContext, module: str) -> Path:
    task = build_analysis_task(context.root, context.settings, module,
                               focus=getattr(context.args, "focus", ""))
    return task.output_path()

def _implement(context: CampaignContext, items: list[tuple[Direction, Path, str]]):
    if not items:
        context.round_progress = 0
        context.recovery_blocked = False
        return []
    rejected, concurrency = _implementation_admission(context, items)
    if rejected is not None:
        return rejected
    outcomes, elapsed = _execute_pipeline_batch(context, items, concurrency)
    _record_round_metrics(context, outcomes, elapsed, concurrency)
    return outcomes


def _implementation_admission(context, items):
    reason, evidence = _provider_breaker_admission(
        getattr(context.args, "provider", ""),
        getattr(context.args, "agent_bin", ""))
    if reason:
        log.error("CAMPAIGN: refusing %d pipeline admission(s): %s", len(items), reason)
        return [_admission_outcome(item, PipelineStatus.PROVIDER_BREAKER_PAUSED,
                                   reason, evidence) for item in items], 0
    remaining = _available_invocations()
    if remaining == 0:
        reason = "campaign invocation budget exhausted before implementation"
        return [_admission_outcome(item, PipelineStatus.BUDGET_BLOCKED, reason)
                for item in items], 0
    configured = context.settings.pipeline_concurrency
    concurrency = min(configured, remaining) if remaining is not None else configured
    context.effective_pipeline_concurrency = max(1, concurrency)
    return None, context.effective_pipeline_concurrency


def _admission_outcome(item, status, reason, evidence=()):
    direction, analysis, _fingerprint = item
    return PipelineOutcome(direction, status, reason, 0,
                           [str(analysis), *evidence])


def _execute_pipeline_batch(context, items, concurrency):
    for direction, analysis, fingerprint in items:
        _direction_event(context, direction, "RUNNING", fingerprint, analysis)
    started = time.monotonic()
    if context.settings.pipeline_concurrency > 1:
        raw = [(direction, analysis) for direction, analysis, _ in items]
        outcomes = run_directions_isolated(
            context.root, context.settings, raw, concurrency, _agent_args(context.args))
    else:
        outcomes = [_run_direction_safe(context, direction, analysis)
                    for direction, analysis, _ in items]
    fingerprints = {direction.direction_id: fingerprint
                    for direction, _analysis, fingerprint in items}
    record_campaign_outcomes(context, outcomes, fingerprints, _event)
    context.round_progress = sum(
        outcome.status == PipelineStatus.PASSED for outcome in outcomes)
    context.recovery_blocked = any(
        getattr(outcome, "recovery_status", "") in {"BLOCKED", "PRESERVED"}
        for outcome in outcomes)
    return outcomes, time.monotonic() - started


def _record_round_metrics(context, outcomes, elapsed, concurrency):
    events_file = getattr(context.args, "events_file", "") or metering.EVENTS_FILE
    run_id = getattr(metering, "RUN_ID", "")
    usage = read_usage(events_file, run_id=run_id)
    attempts, retries = execution_attempts(events_file, run_id)
    metrics = build_efficiency_metrics(
        outcomes, elapsed, concurrency, usage, attempts=attempts, retries=retries)
    metrics["analysis_wall_seconds"] = round(
        max(0.0, float(getattr(context, "analysis_elapsed", 0.0))), 3)
    extra = _round_metrics_identity(context, metrics)
    _event(context, "__campaign__", "__metrics__", "", "CAMPAIGN_METRICS", "",
           [events_file] if events_file else [],
           reason="bounded round efficiency observation", extra=extra)


def _round_metrics_identity(context, metrics):
    concurrency = getattr(context, "effective_pipeline_concurrency",
                          context.settings.pipeline_concurrency)
    return {
        "metrics": metrics, "round_id": getattr(context, "round_id", ""),
        "head_commit": str(context.snapshot.get("head", ""))[:128],
        "model": str(getattr(context.args, "model", ""))[:120],
        "provider": str(getattr(context.args, "provider", ""))[:120],
        "configured_concurrency": context.settings.pipeline_concurrency,
        "effective_concurrency": concurrency,
        "config_fingerprint": digest({
            "settings": context.settings.__dict__,
            "model": getattr(context.args, "model", ""),
            "provider": getattr(context.args, "provider", ""),
            "thinking": getattr(context.args, "thinking", ""),
        }),
    }


def _run_direction_safe(context: CampaignContext, direction: Direction,
                        analysis: Path):
    from .campaign_pipeline import PipelineOutcome
    try:
        return run_direction(context.root, context.settings, direction, analysis,
                             context.args.model, context.args.provider, context.args.timeout,
                             _reuse_enabled(context.args))
    except Exception as exc:
        return PipelineOutcome(direction, "PIPELINE_FAILED", str(exc), 0, [str(analysis)])

def _agent_args(args: argparse.Namespace) -> list[str]:
    values = ["--agent-bin", args.agent_bin, "--memory-mode", args.memory_mode,
              "--stream-output", args.stream_output]
    for flag, value in (("--model", args.model), ("--provider", args.provider),
                        ("--thinking", getattr(args, "thinking", "")),
                        ("--timeout", args.timeout)):
        if value:
            values.extend([flag, str(value)])
    for flag, value in (("--rate-limit", getattr(args, "rate_limit", 0)),
                        ("--rate-burst", getattr(args, "rate_burst", 0))):
        if value and value > 0:
            values.extend([flag, str(value)])
    return values

def _event(context: CampaignContext, module: str, direction_id: str, direction: str,
           status: str, fingerprint: str, evidence: list[str], reason: str = "",
           elapsed: float = 0, extra: dict | None = None) -> None:
    payload = {"campaign": context.settings.name, "module": module,
               "direction_id": direction_id, "direction": direction, "status": status,
               "fingerprint": fingerprint, "evidence": evidence, "reason": reason,
               "elapsed": elapsed,
               "run_id": getattr(context, "run_id", getattr(metering, "RUN_ID", "")),
               "round_id": getattr(context, "round_id", "")}
    payload.update(extra or {})
    context.store.append(payload)
    from .memory import record_campaign
    record_campaign(payload, str(context.root))

def _direction_event(context: CampaignContext, direction: Direction, status: str,
                     fingerprint: str, analysis: Path, reason: str = "",
                     extra: dict | None = None) -> None:
    _event(context, direction.module, direction.direction_id, direction.title,
           status, fingerprint, [str(analysis)], reason, extra=extra)

def _safe_nonnegative_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _reuse_enabled(args: argparse.Namespace) -> bool:
    return not getattr(args, "force", False) and (
        getattr(args, "reuse", False) or getattr(args, "skip_passed", False)
        or getattr(args, "retry_failed", False))

def _campaign_plan(context: CampaignContext, modules: list[str]):
    args = context.args
    events_file = getattr(args, "events_file", "") or metering.EVENTS_FILE
    usage = read_usage(events_file, run_id=getattr(metering, "RUN_ID", ""))
    history = read_usage(events_file)
    baseline = (history.observed_average or
                context.settings.analysis_token_estimate)
    pending = _pending_analysis_modules(context, modules)
    focus = getattr(args, "focus", "")
    if not pending:
        return no_pending_plan(modules, focus=focus, usage=usage,
                               token_estimate=context.settings.analysis_token_estimate,
                               baseline_tokens=baseline)
    cli_batch = getattr(args, "analysis_batch_size", None)
    cli_budget = getattr(args, "analysis_budget_tokens", None)
    batch_size = (context.settings.analysis_batch_size
                  if cli_batch is None else cli_batch)
    budget = (context.settings.analysis_budget_tokens
              if cli_budget is None else cli_budget)
    return plan_modules(
        pending, focus=focus, batch_size=batch_size,
        token_budget=budget, usage=usage,
        token_estimate=context.settings.analysis_token_estimate,
        priority=_strategy_priority(context.store),
        signals=(read_module_signals(context.root, pending)
                 if str(focus).strip() else {}),
        max_calls=_available_invocations(), baseline_tokens=baseline)


def _available_invocations() -> int | None:
    limits = []
    if metering.BUDGET_MAX:
        limits.append(max(0, metering.BUDGET_MAX - metering.INVOCATIONS))
    if metering.DAILY_BUDGET:
        limits.append(max(0, metering.DAILY_BUDGET - metering.budget_spent_today()))
    return min(limits) if limits else None


def _pending_analysis_modules(context: CampaignContext,
                              modules: list[str]) -> list[str]:
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


def _strategy_priority(store: StateStore) -> dict[str, int]:
    """Prefer failures, then give untouched modules a coverage turn."""
    priority: dict[str, int] = {}
    urgent = {"ANALYSIS_FAILED": 20, "PIPELINE_FAILED": 10,
              "GATE_REJECTED": 10, "VALIDATION_FAILED": 10,
              PipelineStatus.BUDGET_BLOCKED: 10}
    for event in store.latest().values():
        owner = str(event.get("campaign", ""))
        if owner and owner != store.campaign:
            continue
        module = str(event.get("module", ""))
        if not module:
            continue
        status = str(event.get("status", ""))
        if event.get("direction_id") == "__analysis__" and status == "ANALYZED":
            priority.setdefault(module, -100)
        weight = urgent.get(status, 0)
        if weight:
            priority[module] = max(priority.get(module, 0), weight)
    return priority


def _print_plan(context: CampaignContext, modules: list[str], plan=None) -> None:
    analysis_sec = context.store.median_elapsed(("ANALYZED",), context.settings.analysis_minutes * 60)
    pipeline_sec = context.store.median_elapsed(
        (PipelineStatus.PASSED, PipelineStatus.GATE_REJECTED,
                 PipelineStatus.PIPELINE_FAILED, PipelineStatus.VALIDATION_FAILED,
                 PipelineStatus.PROVIDER_BREAKER_PAUSED,
                 PipelineStatus.BUDGET_BLOCKED),
        context.settings.pipeline_minutes * 60)
    analysis_wall = len(modules) * analysis_sec / context.settings.analysis_jobs
    direction_count = (context.settings.max_total_directions or
                       len(modules) * context.settings.max_directions)
    pipeline_wall = direction_count * pipeline_sec / context.settings.pipeline_concurrency
    print(f"== Campaign: {context.settings.name} ==")
    total = context.settings.max_total_directions or "per-module"
    print(f"Modules: {len(modules)}; directions/module: {context.settings.max_directions}; "
          f"global directions: {total}")
    if plan is not None:
        usage = plan.usage
        print(f"Strategy: {plan.reason}; estimated analysis tokens: {plan.estimated_tokens}")
        print("Telemetry: calls=%d input=%d output=%d total=%d cost=$%.4f" %
              (usage.calls, usage.input_tokens, usage.output_tokens,
               usage.total_tokens, usage.cost_usd))
        for warning in usage.diagnostics(plan.baseline_tokens or plan.estimated_tokens):
            print("Telemetry warning: " + warning)
        if plan.skipped:
            print("Skipped modules: " + ", ".join(plan.skipped))
    for module in modules:
        print(f"  - {module}")
    print(f"Estimated analysis: {analysis_wall / 60:.0f} min | "
          f"pipelines: {pipeline_wall / 60:.0f} min | "
          f"total: {(analysis_wall + pipeline_wall) / 60:.0f} min")
    print("Dry-run made no files or state changes.")



from .campaign_cli import (  # noqa: E402
    CampaignSignalInterrupt,
    _campaign_sigterm,
    _parse_campaign,
    _register_campaign_run,
    _round_commit,
    _run_campaign_supervised,
    _snapshot_excludes,
    _wire_runtime,
    main,
    run_campaign_loop,
)
