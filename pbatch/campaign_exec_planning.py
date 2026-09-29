"""Planning helpers for the campaign execution coordinator."""

from __future__ import annotations

from typing import Any, Callable

from pbatch.pipeline_status import PipelineStatus

from . import metering
from .campaign_state import StateStore, digest
from .campaign_strategy import (
    no_pending_plan, plan_modules, read_module_signals, read_usage,
)


def _build_campaign_plan(context: Any, modules: list[str],
                         pending_analysis_modules: Callable) -> Any:
    args = context.args
    events_file = getattr(args, "events_file", "") or metering.EVENTS_FILE
    usage = read_usage(events_file, run_id=getattr(metering, "RUN_ID", ""))
    history = read_usage(events_file)
    baseline = (history.observed_average or
                context.settings.analysis_token_estimate)
    pending = pending_analysis_modules(context, modules)
    focus = getattr(args, "focus", "")
    if not pending:
        return no_pending_plan(
            modules, focus=focus,
            token_estimate=context.settings.analysis_token_estimate,
            baseline_tokens=baseline, usage=usage)
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


def _print_plan(context: Any, modules: list[str], plan=None) -> None:
    analysis_sec = context.store.median_elapsed(
        ("ANALYZED",), context.settings.analysis_minutes * 60)
    pipeline_sec = context.store.median_elapsed(
        (PipelineStatus.PASSED, PipelineStatus.GATE_REJECTED,
         PipelineStatus.PIPELINE_FAILED, PipelineStatus.VALIDATION_FAILED,
         PipelineStatus.PROVIDER_BREAKER_PAUSED, PipelineStatus.BUDGET_BLOCKED),
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
        token_values = ((usage.input_tokens, usage.output_tokens, usage.total_tokens)
                        if usage.token_telemetry in {"observed", "partial"}
                        else ("unknown", "unknown", "unknown"))
        print("Telemetry: calls=%d input=%s output=%s total=%s cost=$%.4f (%s)" %
              (usage.calls, *token_values, usage.cost_usd,
               usage.token_telemetry))
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
