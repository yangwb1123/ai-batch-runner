"""Numeric session counters preserve explicit currency through cumulative deltas.

A usage-bearing assistant message is an observed response, not a provider
request. No default currency is imposed on provider pricing metadata.

These are derived observations for the versioned task_finish/task_fail event
ledger (event_id, timestamp, run/task/attempt, raw counters), not mutations to
provider bills. The cumulative snapshots are copied, never modified by the
projection. Its version/basis and source counters retain the audit provenance.
"""
from __future__ import annotations

import re

from .task_billing import _currency
from .task_center_metrics import _cost, number

_PREFIX = "cost_currency_messages_"


def fold_units(message: dict, total: dict) -> None:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    total["_usage_message_records"] = total.get("_usage_message_records", 0) + 1
    if message.get("role") != "assistant":
        return
    total["assistant_usage_messages"] = total.get("assistant_usage_messages", 0) + 1
    currency = _currency(usage)
    amount = _cost(usage)
    if currency and amount is not None:
        key = _PREFIX + currency
        total[key] = total.get(key, 0) + 1
        amount_key = "_explicit_cost_amount_" + currency
        total[amount_key] = total.get(amount_key, 0) + amount


def annotate_units(usage: dict | None) -> dict | None:
    if usage is None:
        return None
    value = dict(usage)
    responses = number(value.get("assistant_usage_messages"), True)
    currencies = {key[len(_PREFIX):]: number(count, True) for key, count in value.items()
        if key.startswith(_PREFIX) and re.fullmatch(r"[A-Z]{3}", key[len(_PREFIX):])}
    priced = {currency: count for currency, count in currencies.items() if count is not None and count > 0}
    records = number(value.get("_usage_message_records"), True)
    if responses and records == responses and len(priced) == 1 and next(iter(priced.values())) == responses:
        currency = next(iter(priced))
        amount = number(value.get("_explicit_cost_amount_" + currency))
        if amount is not None:
            for key in ("cost", "total_cost", "cost_usd"):
                value.pop(key, None)
            value.update(cost_currency=currency, cost_total=amount,
                         cost_projection_version=1,
                         cost_projection_basis="explicit_unit_on_every_delta_usage_record")
    return value
