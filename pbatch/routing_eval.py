"""Evidence-based routing quality report over bounded agent-run observations."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Optional

from . import agent_ledger, config

_MAX_LIMIT = agent_ledger.QUERY_MAX_RECORDS


def routing_report(*, data_dir=None, limit: int = _MAX_LIMIT, agent: str = "",
                   model: str = "", provider: str = "", role: str = "",
                   source: str = "", tier: str = "", requested_tier: str = "",
                   since_ts: str = "", baseline_model: str = "",
                   candidate_model: str = "", baseline_provider: str = "",
                   candidate_provider: str = "") -> dict:
    """Aggregate observed latency, outcomes, retries, and provider usage."""
    root = data_dir or config.data_dir()
    record_filter = _observation_filter(
        model, provider, role, source, tier, requested_tier,
        baseline_model, candidate_model, baseline_provider,
        candidate_provider)
    snapshot = agent_ledger.query_snapshot(
        root, agent=agent, limit=limit, since_ts=since_ts or None,
        record_filter=record_filter)
    groups, validations, unattributed = _partition(snapshot.records)
    rejected = {key for key, events in validations.items()
                if any(event.get("status") == "rejected" for event in events)}
    routes = _aggregate_routes(groups, validations)
    routes = [item for item in routes if _matches(
        item, model=model, provider=provider, role=role,
        source=source, tier=tier, requested_tier=requested_tier)]
    return {
        "schema_version": 1,
        "source": "agent_ledger",
        "source_path": snapshot.path,
        "records_read": len(snapshot.records),
        "records_skipped": snapshot.skipped_records,
        "truncated": snapshot.truncated,
        "unattributed_execution_records": unattributed,
        "validation_reject_tasks": len(rejected),
        "filters": {"agent": agent, "model": model, "provider": provider,
                    "role": role, "source": source, "tier": tier,
                    "requested_tier": requested_tier, "since": since_ts},
        "routes": routes,
        "comparison": (_paired_comparison(
            _filter_comparison_records(snapshot.records, role, source, tier,
                                       requested_tier),
            baseline_model, candidate_model, baseline_provider,
            candidate_provider)
                       if baseline_model and candidate_model else None),
        "error": snapshot.error,
    }


def _observation_filter(model: str, provider: str, role: str,
                        source: str, tier: str, requested_tier: str,
                        baseline_model: str, candidate_model: str,
                        baseline_provider: str, candidate_provider: str):
    paired = bool(baseline_model and candidate_model)
    selectors = ((baseline_model, baseline_provider),
                 (candidate_model, candidate_provider))

    def matches(record: dict) -> bool:
        return _record_matches_filters(
            _route(record), (model, provider, role, source, tier, requested_tier),
            selectors, paired)

    return matches


def _record_matches_filters(route: tuple, filters: tuple, selectors: tuple,
                            paired: bool) -> bool:
    dimensions = ((0, filters[0]), (1, filters[1]), (3, filters[2]),
                  (4, filters[3]), (5, filters[4]), (6, filters[5]))
    if not all(not selected or route[index] == selected
               for index, selected in dimensions):
        return False
    return not paired or any(_route_matches_selector(route, selector)
                             for selector in selectors)


def _route_matches_selector(route: tuple, selector: tuple[str, str]) -> bool:
    model, provider = selector
    return route[0] == model and (not provider or route[1] == provider)


def _partition(records: tuple[dict, ...]) -> tuple[dict, dict, int]:
    executions: dict[tuple[str, str], list[dict]] = defaultdict(list)
    validations: dict[tuple[str, str], list[dict]] = defaultdict(list)
    unattributed = 0
    for record in records:
        task_key = _task_key(record)
        phase = record.get("phase", "execution")
        if phase == "validation":
            if task_key:
                validations[task_key].append(record)
            continue
        if phase != "execution":
            continue
        if not task_key:
            unattributed += 1
            continue
        executions[task_key].append(record)
    return executions, validations, unattributed


def _task_key(record: dict) -> tuple[str, str] | None:
    task_id = str(record.get("task_id", "") or "")
    return (str(record.get("run_id", "") or ""), task_id) if task_id else None


def _aggregate_routes(groups: dict, validations: dict) -> list[dict]:
    accumulators = {}
    rejected = {key for key, events in validations.items()
                if any(event.get("status") == "rejected" for event in events)}
    for task_key, attempts in groups.items():
        final = attempts[-1]
        final_route = _route(final)
        stats = _stats_for(accumulators, final_route)
        stats["tasks"] += 1
        status = str(final.get("status", "failed"))
        stats["succeeded"] += status == "succeeded"
        stats["cancelled"] += status == "cancelled"
        stats["failed"] += status not in {"succeeded", "cancelled"}
        is_reworked = len(attempts) > 1 or task_key in rejected
        stats["reworked_tasks"] += is_reworked
        stats["retried_tasks"] += len(attempts) > 1
        stats["validator_rejected_tasks"] += task_key in rejected
        for attempt in attempts:
            _record_call(_stats_for(accumulators, _route(attempt)), attempt)
        outcomes = validations.get(task_key, ())
        for outcome in outcomes:
            outcome_stats = _stats_for(accumulators, _route(outcome))
            outcome_stats["validation_observations"] += 1
            outcome_stats["validation_passes"] += outcome.get("status") == "succeeded"
            outcome_stats["validation_rejects"] += outcome.get("status") == "rejected"
        if outcomes:
            final_validation = _stats_for(accumulators, _route(outcomes[-1]))
            final_validation["validated_tasks"] += 1
            final_validation["validator_pass_tasks"] += (
                outcomes[-1].get("status") == "succeeded")
    return [_finish_route(key, value) for key, value in
            sorted(accumulators.items())]


def _route(record: dict) -> tuple[str, ...]:
    base = tuple(str(record.get(key, "") or "unspecified")
                 for key in ("model", "provider", "agent", "role",
                             "route_source", "route_tier",
                             "route_requested_tier"))
    downgraded = record.get("route_downgraded")
    mode = record.get("route_profile_mode")
    return (*base, str(downgraded).lower() if isinstance(downgraded, bool)
            else "unspecified", str(mode) if mode is not None else "unspecified")


def _stats_for(accumulators: dict, route: tuple[str, ...]) -> dict:
    return accumulators.setdefault(route, {
        "tasks": 0, "succeeded": 0, "failed": 0, "cancelled": 0,
        "reworked_tasks": 0, "retried_tasks": 0,
        "validator_rejected_tasks": 0,
        "validation_observations": 0, "validation_passes": 0,
        "validation_rejects": 0, "validated_tasks": 0,
        "validator_pass_tasks": 0,
        "calls": 0, "successful_calls": 0, "latency_ms": [],
        "cost_observations": 0, "cost_observed_sum": 0.0,
        "token_observations": 0, "token_observed_sum": 0.0,
    })


def _record_call(stats: dict, record: dict) -> None:
    stats["calls"] += 1
    stats["successful_calls"] += record.get("status") == "succeeded"
    elapsed = record.get("elapsed_ms")
    if _finite_number(elapsed) and elapsed >= 0:
        stats["latency_ms"].append(float(elapsed))
    usage = record.get("usage")
    cost = _usage_value(usage, ("cost_total", "total_cost", "cost"))
    tokens = _token_total(usage)
    if cost is not None and cost >= 0:
        stats["cost_observations"] += 1
        stats["cost_observed_sum"] += cost
    if tokens is not None and tokens >= 0:
        stats["token_observations"] += 1
        stats["token_observed_sum"] += tokens


def _usage_value(usage: object, keys: tuple[str, ...]) -> Optional[float]:
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = usage.get(key)
        if _finite_number(value):
            return float(value)
    return None


def _token_total(usage: object) -> Optional[float]:
    total = _usage_value(usage, ("totalTokens", "total_tokens", "total", "tokens"))
    if total is not None:
        return total
    inputs = _usage_value(usage, ("inputTokens", "input_tokens"))
    outputs = _usage_value(usage, ("outputTokens", "output_tokens"))
    return inputs + outputs if inputs is not None and outputs is not None else None


def _nonnegative(value: Optional[float]) -> Optional[float]:
    return value if value is not None and value >= 0 else None


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value)) and abs(float(value)) <= 1e15
    except (OverflowError, ValueError):
        return False


def _finish_route(route: tuple[str, ...], stats: dict) -> dict:
    calls, tasks = stats["calls"], stats["tasks"]
    latencies = stats.pop("latency_ms")
    costs, tokens = stats["cost_observations"], stats["token_observations"]
    cost_sum, token_sum = stats.pop("cost_observed_sum"), stats.pop("token_observed_sum")
    stats.update({
        "model": route[0], "provider": route[1], "agent": route[2],
        "role": route[3], "route_source": route[4], "route_tier": route[5],
        "route_requested_tier": route[6],
        "route_downgraded": (None if route[7] == "unspecified"
                              else route[7] == "true"),
        "route_profile_mode": route[8],
        "success_rate": _ratio(stats["succeeded"], tasks),
        "call_success_rate": _ratio(stats["successful_calls"], calls),
        "rework_rate": _ratio(stats["reworked_tasks"], tasks),
        "retry_rate": _ratio(stats["retried_tasks"], tasks),
        "validator_pass_rate": _ratio(stats["validator_pass_tasks"],
                                       stats["validated_tasks"]),
        "validator_attempt_pass_rate": _ratio(stats["validation_passes"],
                                               stats["validation_observations"]),
        "validated_tasks": stats["validated_tasks"],
        "validation_observations": stats["validation_observations"],
        "mean_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "median_latency_ms": round(statistics.median(latencies), 2) if latencies else None,
        "cost_total": round(cost_sum, 6) if calls and costs == calls else None,
        "cost_observed_sum": round(cost_sum, 6) if costs else None,
        "cost_coverage": _ratio(costs, calls),
        "tokens_total": round(token_sum, 2) if calls and tokens == calls else None,
        "token_observed_sum": round(token_sum, 2) if tokens else None,
        "token_coverage": _ratio(tokens, calls),
    })
    return stats


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def _matches(route: dict, *, model: str, provider: str, role: str,
             source: str, tier: str, requested_tier: str) -> bool:
    return all(not selected or route[key] == selected for key, selected in (
        ("model", model), ("provider", provider), ("role", role),
        ("route_source", source), ("route_tier", tier),
        ("route_requested_tier", requested_tier)))


def _filter_comparison_records(records: tuple[dict, ...], role: str,
                               source: str, tier: str,
                               requested_tier: str) -> tuple[dict, ...]:
    return tuple(record for record in records
                 if (not role or record.get("role") == role)
                 and (not source or record.get("route_source") == source)
                 and (not tier or record.get("route_tier") == tier)
                 and (not requested_tier
                      or record.get("route_requested_tier") == requested_tier))


def _paired_comparison(records: tuple[dict, ...], baseline: str,
                       candidate: str, baseline_provider: str = "",
                       candidate_provider: str = "") -> dict:
    selectors = ((baseline, baseline_provider), (candidate, candidate_provider))
    executions, validations = _index_comparison_records(records, selectors)
    sides = _comparison_sides(executions, validations, selectors)
    paired = sorted(set(sides["baseline"]) & set(sides["candidate"]))
    metrics = _paired_metrics(sides, paired)
    return {"baseline_model": baseline, "candidate_model": candidate,
            "baseline_provider": baseline_provider or "any",
            "candidate_provider": candidate_provider or "any",
            "paired_fingerprints": len(paired), "metrics": metrics,
            "quality_note": "validator_pass_rate is a deterministic gate proxy, "
                            "not a semantic quality score"}


def _index_comparison_records(records: tuple[dict, ...], selectors: tuple) -> tuple[dict, dict]:
    executions: dict[tuple[str, str, str, tuple[str, str]], list[dict]] = defaultdict(list)
    validations: dict[tuple[str, str, str, tuple[str, str]], list[dict]] = defaultdict(list)
    for record in records:
        _index_comparison_record(record, selectors, executions, validations)
    return executions, validations


def _index_comparison_record(record: dict, selectors: tuple, executions: dict,
                             validations: dict) -> None:
    fingerprint = str(record.get("task_fingerprint", "") or "")
    task_key = _task_key(record)
    if not fingerprint or not task_key:
        return
    if not any(_model_route_matches(record, selector) for selector in selectors):
        return
    key = (fingerprint, str(record.get("model", "") or ""),
           str(record.get("provider", "") or ""), task_key)
    target = validations if record.get("phase") == "validation" else executions
    target[key].append(record)


def _comparison_sides(executions: dict, validations: dict,
                      selectors: tuple) -> dict:
    return {
        "baseline": _comparison_side(executions, validations, selectors[0]),
        "candidate": _comparison_side(executions, validations, selectors[1]),
    }


def _paired_metrics(sides: dict, paired: list[str]) -> dict:
    metrics = {}
    for name in ("success_rate", "validator_pass_rate", "rework_rate",
                 "mean_latency_ms", "cost_per_task"):
        pairs = _metric_pairs(sides, paired, name)
        metrics[name] = _metric_summary(pairs)
    return metrics


def _metric_pairs(sides: dict, paired: list[str], name: str) -> list[tuple]:
    pairs = [(sides["baseline"][fp].get(name),
              sides["candidate"][fp].get(name)) for fp in paired]
    return [(left, right) for left, right in pairs
            if left is not None and right is not None]


def _metric_summary(pairs: list[tuple]) -> dict:
    left_mean = _mean(left for left, _ in pairs)
    right_mean = _mean(right for _, right in pairs)
    delta = round(right_mean - left_mean, 4) if (
        left_mean is not None and right_mean is not None) else None
    return {"baseline_mean": left_mean, "candidate_mean": right_mean,
            "candidate_minus_baseline": delta,
            "paired_fingerprints": len(pairs)}


def _model_route_matches(record: dict, selector: tuple[str, str]) -> bool:
    model, provider = selector
    return record.get("model") == model and (
        not provider or record.get("provider") == provider)


def _comparison_side(executions: dict, validations: dict,
                     selector: tuple[str, str]) -> dict:
    tasks = _group_comparison_tasks(executions, selector)
    output = {}
    for fingerprint, groups in tasks.items():
        observations, validation_values = _comparison_observations(
            groups, validations)
        output[fingerprint] = _comparison_summary(observations, validation_values)
    return output


def _group_comparison_tasks(executions: dict, selector: tuple[str, str]) -> dict:
    model, provider = selector
    tasks: dict[str, list[tuple[tuple[str, str, str, tuple[str, str]], list[dict]]]] = defaultdict(list)
    for (fingerprint, route_model, route_provider, task_key), attempts in executions.items():
        if route_model == model and (not provider or route_provider == provider):
            key = (fingerprint, route_model, route_provider, task_key)
            tasks[fingerprint].append((key, attempts))
    return tasks


def _comparison_observations(groups: list, validations: dict) -> tuple[list, list]:
    observations = []
    validation_values = []
    for key, attempts in groups:
        outcomes = validations.get(key, ())
        observations.append(_comparison_observation(attempts, outcomes))
        if outcomes:
            validation_values.append(outcomes[-1].get("status") == "succeeded")
    return observations, validation_values


def _comparison_observation(attempts: list, outcomes: list) -> dict:
    cost_values = [_nonnegative(_usage_value(
        row.get("usage"), ("cost_total", "total_cost", "cost")))
        for row in attempts]
    elapsed = [row.get("elapsed_ms") for row in attempts
               if _finite_number(row.get("elapsed_ms"))]
    return {
        "success": attempts[-1].get("status") == "succeeded",
        "reworked": len(attempts) > 1 or any(
            row.get("status") == "rejected" for row in outcomes),
        "latency": sum(elapsed) if elapsed else None,
        "cost": sum(cost_values) if cost_values and all(
            value is not None for value in cost_values) else None,
    }


def _comparison_summary(observations: list, validation_values: list) -> dict:
    return {
        "success_rate": _ratio(sum(item["success"] for item in observations),
                               len(observations)),
        "rework_rate": _ratio(sum(item["reworked"] for item in observations),
                              len(observations)),
        "mean_latency_ms": _mean(item["latency"] for item in observations),
        "cost_per_task": _mean(item["cost"] for item in observations),
        "validator_pass_rate": _ratio(sum(validation_values),
                                       len(validation_values)),
    }


def _mean(values) -> Optional[float]:
    included = [float(value) for value in values if _finite_number(value)]
    return round(sum(included) / len(included), 4) if included else None


def routing_eval_main(argv: Optional[list[str]] = None) -> int:
    from .routing_eval_cli import routing_eval_main as main
    return main(argv)
