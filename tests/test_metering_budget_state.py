"""Daily invocation counters fail open on malformed state shapes."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from pbatch import metering


def test_daily_counter_ignores_invalid_json_shapes_and_counts(tmp_path, monkeypatch):
    state = tmp_path / "daily-budget.json"
    monkeypatch.setattr(metering, "_DAILY_STATE", str(state))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    invalid_values = [
        [], None, True,
        {"date": today, "count": "many"},
        {"date": today, "count": -1},
        {"date": today, "count": True},
    ]

    for value in invalid_values:
        state.write_text(json.dumps(value), encoding="utf-8")
        assert metering.budget_spent_today() == 0

    state.write_text(json.dumps({"date": today, "count": 7}), encoding="utf-8")
    assert metering.budget_spent_today() == 7
