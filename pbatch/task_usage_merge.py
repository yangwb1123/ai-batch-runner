"""Complement duplicate telemetry; conflicting measurements remain unknown."""
from __future__ import annotations

from .task_center_metrics import _TOKEN_KEYS, _cost, _tokens, cost_currency, number

_GROUPS = {**_TOKEN_KEYS, "cost": ("cost_total", "total_cost", "cost_usd", "cost"),
    "currency": ("cost_currency", "currency"),
    "provider_requests": ("provider_requests", "provider_requests_basis"),
    "assistant_usage_messages": ("assistant_usage_messages",)}


def _present(value: dict, field: str, keys: tuple) -> bool:
    return any(key in value for key in keys) or (field == "currency"
        and isinstance(value.get("cost"), dict) and "currency" in value["cost"])


def _value(usage: dict, field: str):
    if field in _TOKEN_KEYS:
        return _tokens(usage, field)
    if field == "cost":
        return _cost(usage)
    if field == "currency":
        return cost_currency(usage)
    if field == "provider_requests" and usage.get("provider_requests_basis") != "provider_request_events":
        return None
    return number(usage.get(field), True)


def merge_usage(preferred, fallback):
    if not isinstance(preferred, dict):
        return fallback if preferred is None else preferred
    if not isinstance(fallback, dict):
        return preferred
    result = {**fallback, **preferred}
    conflicts = _retained_conflicts(preferred) | _retained_conflicts(fallback)
    for field, keys in _GROUPS.items():
        if (_present(preferred, field, keys) and _present(fallback, field, keys)
                and _value(preferred, field) != _value(fallback, field)):
            conflicts.add(field)
    for field in conflicts:
        for key in _GROUPS[field]:
            result.pop(key, None)
        if field == "currency" and isinstance(result.get("cost"), dict):
            result["cost"] = {key: value for key, value in result["cost"].items() if key != "currency"}
    if conflicts:
        result["_conflicts"] = sorted(conflicts)
    return result


def _retained_conflicts(usage: dict) -> set:
    items = usage.get("_conflicts")
    return {field for field in items[:16] if isinstance(field, str) and field in _GROUPS} if isinstance(items, list) else set()
