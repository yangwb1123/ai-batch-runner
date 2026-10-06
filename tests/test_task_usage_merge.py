"""Duplicate feed conflicts cannot manufacture billable or token totals."""
from __future__ import annotations

import pytest

from pbatch.task_billing import billing_metrics
from pbatch.task_center_metrics import usage_metrics
from pbatch.task_usage_merge import merge_usage


def metrics(usage):
    rows = [{"phase": "execution", "status": "succeeded", "usage": usage}]
    return usage_metrics(rows, False), billing_metrics(rows, False)


def test_equivalent_aliases_and_complementary_metadata_count_once():
    merged = merge_usage({"totalTokens": 10, "cost_total": 1},
        {"total_tokens": 10, "cost": {"total": 1, "currency": "USD"}, "assistant_usage_messages": 3})
    usage, bill = metrics(merged)
    assert usage["total_tokens"] == 10 and usage["calls"] == 1
    assert bill["comparable_currency"] == "USD" and bill["comparable_total"] == 1
    assert bill["assistant_usage_messages"]["observed_total"] == 3


def test_token_and_price_conflicts_stay_unknown_after_third_duplicate():
    merged = merge_usage({"totalTokens": 10, "cost_total": 1}, {"total_tokens": 20, "cost": 2})
    assert set(merged["_conflicts"]) == {"total_tokens", "cost"}
    usage, bill = metrics(merged)
    assert usage["total_tokens"] is None and usage["cost_total"] is None
    third = merge_usage({"totalTokens": 10, "cost_total": 1}, merged)
    usage, bill = metrics(third)
    assert usage["total_tokens"] is None and bill["comparable_total"] is None


def test_currency_and_request_conflicts_do_not_pick_preferred_facts():
    left = {"cost_total": 1, "currency": "USD", "provider_requests": 1,
        "provider_requests_basis": "provider_request_events"}
    right = {"cost": {"total": 1, "currency": "EUR"}, "provider_requests": 2,
        "provider_requests_basis": "provider_request_events"}
    merged = merge_usage(left, right)
    assert set(merged["_conflicts"]) == {"currency", "provider_requests"}
    _, bill = metrics(merged)
    assert bill["comparable_currency"] is None and bill["provider_requests"]["observed_total"] is None


@pytest.mark.parametrize("bad", [True, {}, "text", 1, None])
def test_untrusted_conflict_metadata_is_bounded_and_never_raises(bad):
    merged = merge_usage({"totalTokens": 1, "_conflicts": bad}, {"total_tokens": 1})
    assert metrics(merged)[0]["total_tokens"] == 1


def test_invalid_preferred_measurement_is_not_silently_repaired_by_fallback():
    merged = merge_usage({"totalTokens": True}, {"total_tokens": 10})
    assert merged["_conflicts"] == ["total_tokens"]
    assert metrics(merged)[0]["total_tokens"] is None
