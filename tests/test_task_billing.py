"""Financial units and response telemetry cannot imply provider request counts."""
from __future__ import annotations

import json

import pytest

from pbatch import agent_ledger, metering
from pbatch.session_usage_units import annotate_units
from pbatch.task_billing import billing_metrics
from pbatch.task_center_metrics import usage_metrics


def row(usage=None, phase="execution"):
    return {"phase": phase, "status": "succeeded", "usage": usage}


def test_currency_totals_never_add_different_units_or_include_validation():
    records = [row({"cost_total": 1, "cost_currency": "USD"}),
        row({"cost_total": 20, "cost_currency": "CNY"}),
        row({"cost_total": 999, "cost_currency": "USD"}, "validation")]
    result = billing_metrics(records, False)
    assert result["currency_totals"] == [{"currency": "CNY", "reported_amount": 20, "observations": 1},
        {"currency": "USD", "reported_amount": 1, "observations": 1}]
    assert result["comparable_total"] is None
    assert usage_metrics(records, False)["cost_total"] is None
    assert usage_metrics(records, False)["cost_currency"] == "mixed_or_partial"


def test_untracked_units_do_not_mix_with_explicit_currency():
    records = [row({"cost_total": 0.5}), row({"cost_total": 1, "currency": "USD"})]
    result = billing_metrics(records, False)
    assert result["financial_coverage"] == "partial"
    assert result["unknown_currency_observations"] == 1
    assert result["comparable_total"] is None
    assert usage_metrics(records, False)["cost_total"] is None


@pytest.mark.parametrize("unit", ["usd", "", "DOLLAR", True, 3, "<script>"])
def test_invalid_units_are_unknown_not_default_usd(unit):
    result = billing_metrics([row({"cost_total": 0, "cost_currency": unit})], False)
    assert result["unknown_currency_observations"] == 1
    assert result["currency_totals"] == []
    assert result["comparable_total"] is None


def test_conflicting_currency_labels_fail_closed():
    usage = {"cost_total": 1, "cost_currency": "USD", "cost": {"total": 1, "currency": "EUR"}}
    result = billing_metrics([row(usage)], False)
    assert result["unknown_currency_observations"] == 1
    assert result["currency_totals"] == []


def test_zero_price_is_reported_not_assumed_and_partial_feed_stays_partial():
    result = billing_metrics([row({"cost": {"total": 0, "currency": "USD"}})], False)
    assert result["comparable_total"] == 0 and result["comparable_currency"] == "USD"
    assert result["financial_coverage"] == "complete_in_observed_rows"
    partial = billing_metrics([row({"cost_total": 1, "cost_currency": "USD"})], True)
    assert partial["financial_coverage"] == "partial" and partial["comparable_total"] is None


def test_cli_calls_and_assistant_messages_are_not_provider_requests():
    records = [row({"assistant_usage_messages": 4})]
    assert usage_metrics(records, False)["calls"] == 1
    result = billing_metrics(records, False)
    assert result["assistant_usage_messages"]["observed_total"] == 4
    assert result["provider_requests"]["observed_total"] is None
    result = billing_metrics([row({"provider_requests": 4})], False)
    assert result["provider_requests"]["observed_total"] is None  # missing required event basis
    result = billing_metrics([row({"provider_requests": 0, "provider_requests_basis": "provider_request_events"})], False)
    assert result["provider_requests"]["observed_total"] == 0
    assert result["provider_request_basis"] == "reported_provider_request_events_only"


@pytest.mark.parametrize("invalid", [True, -1, 1.5, "1", float("nan"), float("inf")])
def test_invalid_request_counts_stay_unknown(invalid):
    result = billing_metrics([row({"provider_requests": invalid, "provider_requests_basis": "provider_request_events"})], False)
    assert result["provider_requests"]["observed_total"] is None
    json.dumps(result, allow_nan=False)


def fold(total, *, role="assistant", currency=None, amount=0.2, **extra):
    cost = {"total": amount}
    if currency is not None:
        cost["currency"] = currency
    usage = {"totalTokens": 10, "cost": cost, **extra}
    message = {"message": {"role": role, "usage": usage}}
    metering._fold_usage_line(json.dumps(message).encode(), total)


def test_session_currency_and_assistant_counts_survive_deltas_and_ledger():
    total = {}
    fold(total, currency="EUR")
    before = dict(total)
    fold(total, currency="USD")
    fold(total, currency="USD")
    usage = metering._event_usage(total, before)
    assert usage["assistant_usage_messages"] == 2
    assert usage["cost_currency"] == "USD"
    assert usage["cost_total"] == pytest.approx(0.4)
    recorded = agent_ledger._usage(usage)
    assert recorded["cost_currency"] == "USD" and recorded["assistant_usage_messages"] == 2
    assert recorded["cost_total"] == pytest.approx(0.4)
    assert "provider_requests" not in recorded


def test_currency_requires_explicit_units_on_all_delta_messages():
    total = {}
    fold(total, currency="USD")
    fold(total)  # known cost but unit absent
    assert "cost_currency" not in annotate_units(total)
    total = {}
    fold(total, currency="USD")
    fold(total, currency="EUR")
    assert "cost_currency" not in annotate_units(total)
    total = {}
    fold(total, currency="USD")
    fold(total, role="tool")  # an unqualified usage record must not price assistant work
    assert "cost_currency" not in annotate_units(total)


def test_bad_cost_cannot_get_currency_certainty_or_free_billing():
    total = {}
    fold(total, currency="USD", amount=True)
    result = metering._event_usage(total, None)
    assert "cost_currency" not in result and "cost_total" not in result
    assert result["assistant_usage_messages"] == 1


def test_numeric_usage_limit_does_not_discard_additive_currency_or_response_metadata():
    usage = {"totalTokens": 10, "cost_total": 1, **{f"other{i}": i for i in range(20)},
        "cost_currency": "CNY", "assistant_usage_messages": 3}
    recorded = agent_ledger._usage(usage)
    assert recorded["cost_currency"] == "CNY" and recorded["assistant_usage_messages"] == 3
    assert len(recorded) <= 12


def test_incremental_real_session_reader_counts_only_new_assistant_usage_records(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr(metering, "SESSIONS_DIR", root)
    monkeypatch.setattr(metering, "_USAGE_CACHE", {})
    directory = root / metering.workdir_key(str(tmp_path))
    directory.mkdir(parents=True)
    path = directory / "date_batch.jsonl"
    message = {"type": "message", "message": {"role": "assistant", "usage": {
        "totalTokens": 10, "cost": {"total": 0.5, "currency": "USD"}}}}
    line = json.dumps(message) + "\n"
    path.write_text(line)
    before = metering.session_usage(str(tmp_path), "batch")
    assert before["assistant_usage_messages"] == 1
    with path.open("a") as handle:
        handle.write(line)
    after = metering.session_usage(str(tmp_path), "batch")
    usage = metering._event_usage(after, before)
    assert usage["assistant_usage_messages"] == 1 and usage["totalTokens"] == 10
    assert usage["cost_currency"] == "USD" and usage["cost_total"] == 0.5
    assert metering.session_usage(str(tmp_path), "batch") == after
    assert "provider_requests" not in usage


def test_ambiguous_aliases_inside_one_report_remain_unknown():
    rows = [row({"cost_total": -1, "cost": {"total": 0, "currency": "USD"},
                 "totalTokens": 10, "total_tokens": 20})]
    assert usage_metrics(rows, False)["cost_total"] is None
    assert usage_metrics(rows, False)["total_tokens"] is None
    assert billing_metrics(rows, False)["missing_amount_observations"] == 1


def test_explicit_pricing_normalizes_cost_aliases_across_assistant_messages():
    total = {}
    metering._fold_usage_line(json.dumps({"message": {"role": "assistant", "usage": {
        "total_cost": 0.5, "currency": "USD"}}}).encode(), total)
    fold(total, currency="USD", amount=0.5)
    usage = metering._event_usage(total, None)
    assert usage["cost_total"] == 1 and usage["cost_currency"] == "USD"
    assert billing_metrics([row(usage)], False)["comparable_total"] == 1
