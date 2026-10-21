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
from .campaign_analysis import build_analysis_task, discover_modules, select_directions
from .campaign_analysis_phase import (_analyze_modules, _pending_analysis_modules,
                                      _reuse_enabled)
from .campaign_budget import context_settings
from .campaign_models import CampaignSettings, Direction, safe_nonnegative_int
from .campaign_exec_planning import (
    _available_invocations, _build_campaign_plan, _print_plan,
)
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
from .campaign_state import (digest, failure_fingerprint, tool_digest,
                             tool_head, write_summary)
from .campaign_strategy import read_usage
from .config import log


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


def _budget_exhausted(context: CampaignContext) -> bool:
    """Stop before spending anything when the campaign budget is spent.

    The budget is anchored to the campaign ledger, not to this process, so a
    supervisor that restarts the campaign cannot hand itself a fresh window.
    """
    from .campaign_budget import expired, state_path_for
    settings = context.settings
    is_expired, reason = expired(settings.budget_seconds, settings.deadline_at,
                                 state_path_for(context.root, settings))
    if not is_expired:
        if reason:
            log.info("CAMPAIGN budget: %s", reason)
        return False
    log.error("CAMPAIGN stopped: %s", reason)
    _event(context, "__campaign__", "__budget__", "", "CAMPAIGN_BUDGET_EXHAUSTED",
           "", [], reason=reason)
    try:
        write_summary(context.store,
                      context.settings.summary_path(context.root),
                      context.settings.name)
    except OSError:
        pass
    return True


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
    if _budget_exhausted(context):
        return 3
    context_settings(context)  # one clock/latch shared by analysis and pipelines
    analysis_scope = set(plan.selected)
    pause = _provider_admission_pause(context)
    if pause is not None:
        return pause
    _preflight_validators(context)
    _event(context, "__campaign__", "__plan__", "", "CAMPAIGN_PLAN", "",
           [], reason=plan.reason, extra={"strategy": plan.mapping()})
    _event(context, "__tool__", "__tool__", "", "TOOL_VERSION",
           tool_digest(), [tool_head()],
           reason="tool code digest (pbatch + pi-batch.py + pi-batch.yaml); "
                  "invalidates analysis/direction reuse when the tool changes")
    directions, failures = _analyze_modules(context, modules, analysis_scope, _event)
    if getattr(context, "analysis_budget_stopped", False):
        return _analysis_budget_stop(context)
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
    return (3 if any(item.status == PipelineStatus.BUDGET_BLOCKED for item in outcomes)
            else 1 if failures else 0)

def _provider_admission_pause(context: CampaignContext) -> int | None:
    reason, evidence = _provider_breaker_admission(
        getattr(context.args, "provider", ""),
        getattr(context.args, "agent_bin", ""))
    if not reason:
        return None
    _event(context, "__provider__", "__provider__", "",
           PipelineStatus.PROVIDER_BREAKER_PAUSED, "", evidence, reason=reason)
    write_summary(context.store, context.settings.summary_path(context.root),
                  context.settings.name)
    log.error("CAMPAIGN paused before analysis: %s", reason)
    return 1


def _analysis_budget_stop(context: CampaignContext) -> int:
    _event(context, "__campaign__", "__budget__", "", "CAMPAIGN_BUDGET_EXHAUSTED",
           "", [], reason="campaign deadline exhausted during analysis; "
                          "no directions selected")
    write_summary(context.store, context.settings.summary_path(context.root),
                  context.settings.name)
    return 3


def _blocked_campaign(context: CampaignContext, plan) -> int:
    _event(context, "__campaign__", "__plan__", "", "CAMPAIGN_PLAN", "",
           [], reason=plan.reason, extra={"strategy": plan.mapping()})
    log.error("CAMPAIGN blocked by module strategy: %s", plan.reason)
    write_summary(context.store,
                  context.settings.summary_path(context.root),
                  context.settings.name)
    return 3


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
    count = (safe_nonnegative_int(previous.get("same_failure_count", 0))
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
    settings = context_settings(context)
    if context.settings.pipeline_concurrency > 1:
        raw = [(direction, analysis) for direction, analysis, _ in items]
        outcomes = run_directions_isolated(
            context.root, settings, raw, concurrency, _agent_args(context.args))
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
    try:
        return run_direction(context.root, context_settings(context), direction, analysis,
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

def _campaign_plan(context: CampaignContext, modules: list[str]):
    return _build_campaign_plan(context, modules, _pending_analysis_modules)





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
