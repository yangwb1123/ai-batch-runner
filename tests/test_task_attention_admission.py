"""Terminal refuses contradictory attention projections; no fallback to old visible facts."""
from __future__ import annotations

import copy

import pytest

from pbatch.task_center_terminal_view import TerminalView
from test_task_attention_interfaces import failed_view
from test_task_center import source


@pytest.mark.parametrize("change", [
    lambda value: value.update(schema_version=2),
    lambda value: value.update(schema_version=True),
    lambda value: value.update(method="unknown"),
    lambda value: value.update(window_only=False),
    lambda value: value.update(observed_items=True),
    lambda value: value.update(observed_items=0),
    lambda value: value.update(omitted_items=-1),
    lambda value: value.update(omitted_items=True),
    lambda value: value.update(grants_execution=True),
    lambda value: value.update(zero_items_proves_safety_or_no_missing_work=True),
    lambda value: value.update(items=value["items"]*129, observed_items=129),
    lambda value: value["items"][0].update(attention_id="not-a-full-identity"),
    lambda value: value["items"][0].update(source_id="src-"+"0"*24),
    lambda value: value["items"][0].update(target={"kind":"tasks","record_id":"/etc/passwd"}),
    lambda value: value["items"][0].update(target={"kind":"unknown","record_id":"tc-"+"0"*32}),
    lambda value: value["items"][0].update(target={"kind":"sources","record_id":"src-"+"0"*24}),
    lambda value: value["items"][0].update(target=None),
])
def test_corruption_or_conflict_refuses_without_preserving_previous_attention(source, change):
    original = failed_view(source)
    model = TerminalView(source["source_id"])
    generation = model.invalidate()
    model.publish(generation, original)
    model.select("attention:"+original["attention"]["items"][0]["attention_id"])
    broken = copy.deepcopy(original); change(broken["attention"])
    with pytest.raises(ValueError):
        model.publish(generation, broken)
    assert model.items == {} and model.snapshot is None
    assert "数据不可用" in model.detail()


def test_explicit_null_is_not_silently_treated_as_an_old_snapshot(source):
    value = failed_view(source); value["attention"] = None
    model = TerminalView(source["source_id"])
    with pytest.raises(ValueError):
        model.publish(model.invalidate(), value)
    assert model.items == {}
