"""Reported financial/request telemetry; never infer currency or request counts."""
from __future__ import annotations

from collections import defaultdict

from .task_center_metrics import _cost, cost_currency as _currency, number

TERMINAL = {"succeeded", "failed", "rejected", "cancelled"}


def execution_rows(rows: list) -> list:
    return [row for row in rows if row["phase"] == "execution" and row["status"] in TERMINAL]


def _count(usage: dict, key: str, required_basis: str | None = None) -> float | None:
    if required_basis and usage.get("provider_requests_basis") != required_basis:
        return None
    return number(usage.get(key), True)


def _count_summary(usages: list, key: str, partial: bool, basis: str | None = None) -> dict:
    values = [_count(usage, key, basis) for usage in usages]
    observed = [value for value in values if value is not None]
    return {"observed_total": int(sum(observed)) if observed else None,
            "covered_execution_observations": len(observed),
            "execution_observations": len(usages),
            "coverage": _coverage(len(observed), len(usages), partial)}


def _coverage(measured: int, count: int, partial: bool) -> str:
    if not measured:
        return "unavailable"
    return "complete_in_observed_rows" if measured == count and not partial else "partial"


def billing_metrics(rows: list, partial: bool) -> dict:
    executions = execution_rows(rows)
    usages = [row["usage"] if isinstance(row.get("usage"), dict) else {} for row in executions]
    amounts = defaultdict(list)
    unknown, missing = 0, 0
    for usage in usages:
        amount, currency = _cost(usage), _currency(usage)
        if amount is None:
            missing += 1
        elif currency is None:
            unknown += 1
        else:
            amounts[currency].append(amount)
    totals = [{"currency": currency, "reported_amount": sum(values), "observations": len(values)}
              for currency, values in sorted(amounts.items())]
    measured = sum(len(values) for values in amounts.values())
    requests = _count_summary(usages, "provider_requests", partial, "provider_request_events")
    responses = _count_summary(usages, "assistant_usage_messages", partial)
    return {"currency_totals": totals, "currency_basis": "explicit_reported_label_not_inferred",
            "financial_coverage": _coverage(measured, len(usages), partial),
            "unknown_currency_observations": unknown, "missing_amount_observations": missing,
            "comparable_total": totals[0]["reported_amount"] if len(totals) == 1 and not (unknown or missing or partial) else None,
            "comparable_currency": totals[0]["currency"] if len(totals) == 1 and not (unknown or missing or partial) else None,
            "provider_requests": requests,
            "provider_request_basis": "reported_provider_request_events_only",
            "assistant_usage_messages": responses,
            "response_basis": "usage_bearing_assistant_message_records_not_provider_requests",
            "window_only": True, "bill_reconciled": False,
            "note": "reported labels are not invoices; no currency conversion or inferred requests"}


def preserved_usage(value: dict) -> dict:
    """Small additive telemetry allowed through the legacy numeric ledger."""
    result = {}
    amount = _cost(value)
    if amount is not None:
        result["cost_total"] = amount
    currency = _currency(value)
    if currency:
        result["cost_currency"] = currency
    for key in ("provider_requests", "assistant_usage_messages"):
        count = _count(value, key, "provider_request_events" if key == "provider_requests" else None)
        if count is not None:
            result[key] = int(count)
    if "provider_requests" in result:
        result["provider_requests_basis"] = "provider_request_events"
    if type(value.get("cost_projection_version")) is int and value["cost_projection_version"] == 1:
        if value.get("cost_projection_basis") == "explicit_unit_on_every_delta_usage_record":
            result.update(cost_projection_version=1, cost_projection_basis=value["cost_projection_basis"])
    return result
