"""Pure terminal projection: complete identities, bounded views, stale-content and authority separation."""
from __future__ import annotations

import copy
import json

import pytest

from pbatch.task_center_terminal_view import (
    KINDS, MAX_DETAIL_BYTES, TerminalView, json_detail, plain, records, row_cells, task_facts,
    validate_options,
)

SOURCE = "src-" + "a" * 24
TASK = "tc-" + "1" * 32


def sample(kind="tasks", source=SOURCE, name="synthetic"):
    view = {key: [] for key in KINDS}
    view.update(schema_version=1, window_only=True, sources=[{"source_id": source, "name": name, "coverage": []}],
                diagnostics=[], observed_task_count=1, omitted_tasks=0)
    field, prefix = KINDS[kind][1:3]
    row = {field: prefix + "-" + "1" * 32, "source_id": source, "project": name,
           "title": "same display", "status": "authorized" if kind == "intents" else "local_draft_only" if kind == "feedback" else "observed_only"}
    if kind == "feedback":
        row.update(state="draft", summary="untrusted quoted case", duplicate_inspection={"observed_matches": 0})
    view[kind] = [row]
    return view


@pytest.mark.parametrize("kind", KINDS)
def test_each_shared_group_has_full_identity_and_no_extra_reads(kind):
    value = sample(kind)
    model = TerminalView()
    generation = model.invalidate()
    assert model.publish(generation, value)
    key = next(iter(model.items))
    model.select(key)
    assert json.loads(model.detail()) == value[kind][0]
    assert row_cells(key, model.items[key])[2] == key.partition(":")[2]
    assert model.snapshot == value


def test_same_redacted_titles_are_not_selection_identity():
    value = sample()
    second = copy.deepcopy(value["tasks"][0]); second["task_id"] = "tc-" + "2" * 32
    value["tasks"].append(second)
    model = TerminalView(); model.publish(model.invalidate(), value)
    model.select("tasks:" + second["task_id"])
    assert json.loads(model.detail())["task_id"] == second["task_id"]
    model.select("same display")
    assert model.selected == "" and "task_id" not in model.detail()


def test_refresh_clear_missing_identity_and_late_result_cannot_restore_old_detail():
    value = sample(); value["tasks"][0]["title"] = "OLD-SOURCE-DETAIL"
    model = TerminalView(task_id=TASK)
    old = model.invalidate(); model.publish(old, value)
    assert "OLD-SOURCE-DETAIL" in model.detail()
    new = model.invalidate()
    assert model.items == {} and model.snapshot is None and "OLD-SOURCE-DETAIL" not in model.detail()
    assert not model.publish(old, value) and not model.fail(old, "OLD-ERROR")
    current = sample(); current["tasks"] = []
    model.publish(new, current)
    assert "不在本次窗口" in model.detail() and "OLD-SOURCE-DETAIL" not in model.detail()


def test_source_switch_clears_selection_and_rejects_out_of_scope_snapshot():
    model = TerminalView(SOURCE, TASK)
    model.publish(model.invalidate(), sample())
    selected = "src-" + "b" * 24
    generation = model.invalidate(selected)
    assert model.selected == "" and model.items == {}
    with pytest.raises(ValueError, match="source filter"):
        model.publish(generation, sample())
    assert model.items == {} and model.snapshot is None
    assert "数据不可用" in model.detail()
    assert model.publish(generation, sample(source=selected, name="second"))


def test_registered_name_filter_and_missing_source_are_not_path_capabilities():
    model = TerminalView("synthetic")
    model.publish(model.invalidate(), sample())
    model.invalidate("/etc/passwd?execute=true")
    empty = {kind: [] for kind in KINDS}
    empty.update(schema_version=1, window_only=True, sources=[], diagnostics=["task source not found"])
    assert model.publish(model.generation, empty) and model.items == {}


@pytest.mark.parametrize("change", [
    lambda view: view.update(schema_version=True), lambda view: view.update(schema_version=2),
    lambda view: view.update(window_only=False), lambda view: view.update(tasks={}),
    lambda view: view.pop("feedback"), lambda view: view.update(sources=None),
    lambda view: view["sources"].append(copy.deepcopy(view["sources"][0])),
    lambda view: view["tasks"].append(copy.deepcopy(view["tasks"][0])),
    lambda view: view["tasks"][0].update(task_id="/etc/passwd"),
    lambda view: view["tasks"][0].update(task_id="tc-" + "g" * 32),
    lambda view: view["tasks"][0].update(source_id="src-" + "b" * 24),
    lambda view: view["tasks"][0].update(amount=float("nan")),
])
def test_malformed_or_ambiguous_snapshot_refuses_and_discards_cached_rows(change):
    model = TerminalView(task_id=TASK); model.publish(model.invalidate(), sample())
    assert "task_id" in model.detail()
    value = sample(); change(value)
    with pytest.raises(ValueError):
        model.publish(model.generation, value)
    assert model.items == {} and model.snapshot is None and "task_id" not in model.detail()


@pytest.mark.parametrize("kind", KINDS)
def test_group_limits_refuse_without_truncating_or_claiming_full_coverage(kind):
    value = sample(kind)
    value[kind] = value[kind] * (KINDS[kind][3] + 1)
    with pytest.raises(ValueError, match="over budget"):
        records(value)


@pytest.mark.parametrize("updates", [
    {"status": "withdrawn"}, {"status": "stale_source_definition"}, {"state": "withdrawn"},
    {"status": "unknown_future_state"}, {"state": "unknown_future_state"},
])
def test_inconsistent_or_inactive_feedback_suppresses_old_summary_and_peer_association(updates):
    value = sample("feedback")
    row = value["feedback"][0]
    row.update(summary="OLD-FEEDBACK-SUMMARY", duplicate_inspection={"matching_feedback_ids": ["OLD-PEER"]}, **updates)
    model = TerminalView(); model.publish(model.invalidate(), value); model.select(next(iter(model.items)))
    shown = json.loads(model.detail())
    assert shown["summary"] is None and shown["duplicate_inspection"] is None
    assert "OLD-FEEDBACK-SUMMARY" not in model.detail() and "OLD-PEER" not in model.detail()
    assert row["summary"] == "OLD-FEEDBACK-SUMMARY"  # never mutate supplied metadata


@pytest.mark.parametrize("status", ["revoked", "stale_contract", "goal_unavailable", "unknown"])
def test_unauthorized_history_analysis_is_suppressed_even_if_client_metadata_conflicts(status):
    value = sample("intents"); value["intents"][0].update(status=status, analysis={"quoted": "OLD-INTENT"})
    model = TerminalView(); model.publish(model.invalidate(), value); model.select(next(iter(model.items)))
    assert json.loads(model.detail())["analysis"] is None and "OLD-INTENT" not in model.detail()


def test_display_quotes_markup_urls_and_commands_and_escapes_terminal_controls():
    marker = "[red]https://example.invalid/; rm -rf quoted[/red]\x1b[31m\x9b31m\u202e"
    value = sample(); value["tasks"][0]["title"] = marker
    model = TerminalView(); model.publish(model.invalidate(), value); key = next(iter(model.items)); model.select(key)
    cells = row_cells(key, model.items[key])
    assert "[red]" in cells[-1] and "rm -rf quoted" in cells[-1]
    assert not any(char in cells[-1] for char in ("\x1b", "\x9b", "\u202e"))
    assert not any(char in model.detail() for char in ("\x1b", "\x9b", "\u202e"))
    assert json.loads(model.detail())["title"] == marker
    assert plain("token=secret_value\x9b") != "token=secret_value\x9b"


def test_detail_utf8_display_budget_is_visible_not_silent_json_salvage():
    value = {"summary": "汉😀" * MAX_DETAIL_BYTES}
    shown = json_detail(value)
    assert len(shown.encode("utf-8")) <= MAX_DETAIL_BYTES + 256
    assert "不是完整 JSON" in shown
    with pytest.raises(ValueError):
        json.loads(shown)


@pytest.mark.parametrize("limit,interval,task_id", [(0,5,""), (201,5,""), (True,5,""),
    (1,float("nan"),""), (1,float("inf"),""), (1,0.5,""), (1,5,"/etc/passwd")])
def test_invalid_terminal_options_refuse_before_any_loader(limit, interval, task_id):
    with pytest.raises(ValueError):
        validate_options(limit, interval, task_id)


def test_retained_fields_do_not_invent_acceptance_eta_billing_or_execution():
    value = sample()
    value["tasks"][0].update(time={"remaining_seconds": None}, efficiency={"accepted_goals": None},
        billing={"comparable_reported_amount": None}, capabilities={"control": False, "cleanup": False})
    model = TerminalView(); model.publish(model.invalidate(), value); model.select(next(iter(model.items)))
    assert json.loads(model.detail()) == value["tasks"][0]
    assert "不等于验收" in model.message and "不是实时 ETA" in model.message


def test_task_facts_reports_bounded_prior_interval_and_never_a_live_eta():
    record = {"flow": {}, "time": {}, "delivery": {},
              "forecast": {"prior_remaining_interval_seconds": [1.5, 4.0],
                           "prior_remaining_basis": "backtested_same_fingerprint_interval_minus_observed_elapsed",
                           "not_a_live_eta": True}}
    text = task_facts(record)
    assert "历史先验剩余区间=1.5\u20134.0 秒" in text
    assert "not_a_live_eta=True" in text and "remaining_seconds/eta 仍为 null" in text
    assert "历史先验剩余区间=未追踪" in task_facts({"flow": {}, "time": {}, "delivery": {}})


def test_view_message_reports_mapping_bounds_without_inference_claims():
    view = sample()
    view["execution_mapping"] = {"verified_spawn_and_outcome": 3,
        "outcome_without_spawn_observation": 2, "spawn_only_active_observations": 1,
        "coverage": "partial_window", "unknown_not_zero": True}
    model = TerminalView()
    assert model.publish(model.invalidate(), view)
    assert "执行映射：已验证=3" in model.message
    assert "缺 spawn 证据=2" in model.message and "计数为下界=True" in model.message
    assert "不等于验收" in model.message and "不是实时 ETA" in model.message
