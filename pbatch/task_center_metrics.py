"""Descriptive per-task metrics; missing telemetry is never free execution.

Only execution attempts consume LLM usage. Validation rows can duplicate that
usage and must not be billed twice. All totals describe the bounded read window.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

_TOKEN_KEYS = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "input"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "output"),
    "cache_tokens": ("cache_tokens", "cacheRead", "cache_read_tokens"),
    "total_tokens": ("total_tokens", "totalTokens", "total"),
}


def number(value: object, integer: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except OverflowError:
        return None
    if not math.isfinite(numeric) or not 0 <= numeric <= 1e15:
        return None
    if integer and not numeric.is_integer():
        return None
    return numeric


def timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return number(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _pick(usage: dict, keys: tuple, integer: bool = False) -> float | None:
    for key in keys:
        if key in usage:
            return number(usage[key], integer)
    return None


def _tokens(usage: dict, field: str) -> float | None:
    observed = _pick(usage, _TOKEN_KEYS[field], True)
    if observed is None and field == "total_tokens":
        incoming = _pick(usage, _TOKEN_KEYS["input_tokens"], True)
        outgoing = _pick(usage, _TOKEN_KEYS["output_tokens"], True)
        if incoming is not None and outgoing is not None:
            return incoming + outgoing  # cache is not an additional token category
    return observed


def _cost(usage: dict) -> float | None:
    reported = _pick(usage, ("cost_total", "total_cost", "cost_usd", "cost"))
    if reported is None and isinstance(usage.get("cost"), dict):
        reported = number(usage["cost"].get("total"))
    return reported


def _coverage(count: int, calls: int, partial_window: bool) -> str:
    if not count:
        return "unavailable"
    return "complete" if count == calls and not partial_window else "partial"


def usage_metrics(rows: list[dict], partial_window: bool) -> dict:
    calls = [row for row in rows if row["phase"] == "execution" and row["status"] in
             {"succeeded", "failed", "rejected", "cancelled"}]
    usages = [row.get("usage") if isinstance(row.get("usage"), dict) else {} for row in calls]
    result = {"calls": len(calls), "window_only": True, "cost_currency": "untracked"}
    for field in _TOKEN_KEYS:
        values = [_tokens(usage, field) for usage in usages]
        observed = [value for value in values if value is not None]
        result[field] = int(sum(observed)) if observed else None
        if field == "total_tokens":
            result["calls_with_tokens"] = len(observed)
            result["token_coverage"] = _coverage(len(observed), len(calls), partial_window)
    costs = [value for usage in usages if (value := _cost(usage)) is not None]
    result.update(cost_total=sum(costs) if costs else None, calls_with_cost=len(costs),
                  cost_coverage=_coverage(len(costs), len(calls), partial_window))
    return result


def time_metrics(rows: list[dict], now: float) -> dict:
    durations = [row["elapsed"] for row in rows if row.get("elapsed") is not None]
    starts = [stamp for row in rows
              if (stamp := timestamp(row.get("started_at"))) is not None]
    stamps = [stamp for row in rows if (stamp := timestamp(row.get("ts"))) is not None]
    start = min(starts) if starts else None
    live = rows[-1]["status"].lower() in {"running", "queued", "retrying", "selected"}
    end = None if live else (max(stamps) if stamps else None)
    boundary = now if live else end
    wall = max(0.0, boundary - start) if boundary is not None and start is not None else None
    return {"started_at": start, "last_observed_at": max(stamps) if stamps else None,
            "elapsed_wall_seconds": wall,
            "reported_work_seconds": sum(durations) if durations else None,
            "waiting_seconds": None, "remaining_seconds": None,
            "forecast_basis": "untracked: no comparable history or trusted remaining DAG",
            "liveness": "not_verified" if live else "last_attempt_reported_terminal"}


def efficiency(usage: dict, accepted: int | None = None) -> dict:
    """No acceptance contract means no denominator, not a zero-cost success."""
    return {"accepted_goals": accepted,
            "tokens_per_accepted_goal": (usage["total_tokens"] / accepted
                if accepted and usage["token_coverage"] == "complete" else None),
            "cost_per_accepted_goal": (usage["cost_total"] / accepted
                if accepted and usage["cost_coverage"] == "complete" else None),
            "note": "execution/validation success is not proof of goal acceptance"}
