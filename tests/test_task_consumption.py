"""Explicit lineage accounting: bounded scope, overlapping goals, no fake acceptance."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import task_center, task_goal_store as goals, task_sources
from pbatch.task_lifecycle import source_identity
from pbatch.task_state_io import atomic_write, locked


@pytest.fixture
def source(tmp_path, monkeypatch):
    home = tmp_path / "private"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(home))
    data, project = home / "projects" / "demo", tmp_path / "project"
    data.mkdir(parents=True)
    project.mkdir()
    value = task_sources.source("demo", str(data), str(project))
    task_sources.register_source(value)
    return value


def run(source, letter="a", parent="", parent_source=""):
    value = {"schema_version": 1, "kind": "pipeline", "run_id": "pl-" + letter * 32,
        "source_id": source_identity(Path(source["data_dir"])), "name": "same title",
        "status": "succeeded", "started_at": 100, "ts": "2026-10-10T00:00:10Z", "seq": 3,
        "plan_complete": True, "steps": [{"id": "implement", "dependencies": [],
            "state": "succeeded", "gate": False}], "stage": "implement",
        "parent_run_id": parent, "parent_source_id": parent_source, "process_run_id": "process"}
    path = Path(source["data_dir"]) / "tasks" / "runs" / (value["run_id"] + ".json")
    with locked(path):
        atomic_write(path, value)
    return value


def rows(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


def goal(source, checkpoint, goal_id="delivery"):
    value = goals.define(source["source_id"], {"schema_version": 1, "goal_id": goal_id,
        "statement": "same title", "criteria": [{"id": "review", "description": "independent review"}]},
        "operator", "confirmed")
    goals.bind(value["goal_key"], checkpoint["run_id"], "operator", "same goal", 1)
    return value


def call(source, checkpoint, **changes):
    return {"run_id": "process", "task_id": "worker", "stage": "implement", "role": "worker",
        "attempt": 1, "phase": "execution", "status": "succeeded",
        "ts": "2026-10-10T00:00:20Z", "elapsed_ms": 1000, "task_brief": "same title",
        "parent_run_id": checkpoint["run_id"], "parent_source_id": checkpoint["source_id"],
        "usage": {"totalTokens": 10, "cost_total": 0.5, "cost_currency": "USD"}, **changes}


def publish(source, checkpoints, calls):
    rows(Path(source["data_dir"]) / "tasks" / "lifecycle.jsonl", checkpoints)
    rows(Path(source["data_dir"]) / "agents" / "ledger.jsonl", calls)


def summary(source, limit=100):
    return task_center.snapshot(limit=limit, source_id=source["source_id"])


def test_goal_rollup_includes_return_omissions_not_validation_or_other_goals(source):
    first, second = run(source), run(source, "b")
    goal(source, first)
    values = [call(source, first, status="failed"), call(source, first, phase="validation", status="failed"),
        call(source, first, attempt=2), call(source, first, attempt=2, phase="validation"),
        call(source, second, task_id="unrelated", usage={"totalTokens": 500})]
    publish(source, [first, second], values)
    small, large = summary(source, 1), summary(source)
    accounting = small["goals"][0]["consumption"]
    assert accounting == large["goals"][0]["consumption"]
    assert accounting["usage"]["calls"] == 2 and accounting["usage"]["total_tokens"] == 20
    assert accounting["billing"]["currency_totals"] == [{"currency": "USD", "reported_amount": 1, "observations": 2}]
    assert accounting["outcomes"]["failed_execution_observations"] == 1
    assert accounting["outcomes"]["failed_validation_observations"] == 1
    assert accounting["outcomes"]["retry_execution_observations"] == 1
    assert accounting["observed_work_seconds"] == 2
    assert accounting["lifetime_complete"] is False
    assert accounting["outcomes"]["independently_accepted_goals"] is None
    assert small["goals"][0]["acceptance"] == "pending"


def test_nested_pipeline_cost_counts_each_call_once_and_inherits_goal(source):
    first = run(source)
    second = run(source, "b", first["run_id"], first["source_id"])
    value = goal(source, first)
    publish(source, [first, second], [call(source, second)])
    view = summary(source)
    accounting = view["goals"][0]["consumption"]
    assert accounting["usage"]["calls"] == 1
    child = next(task for task in view["tasks"] if task["kind"] == "agent")
    assert child["goal_links"][0]["goal_id"] == value["goal_key"]
    pipelines = [task for task in view["tasks"] if task["kind"] == "pipeline"]
    assert all(task["consumption"]["usage"]["calls"] == 1 for task in pipelines)
    assert all(task["consumption"]["allocation"] == "inclusive_not_exclusive_or_apportioned" for task in pipelines)


def test_same_execution_bound_to_two_goals_is_marked_shared_not_apportioned(source):
    checkpoint = run(source)
    goal(source, checkpoint, "first")
    goal(source, checkpoint, "second")
    publish(source, [checkpoint], [call(source, checkpoint)])
    view = summary(source)
    assert len(view["goals"]) == 2
    for value in view["goals"]:
        accounting = value["consumption"]
        assert accounting["usage"]["total_tokens"] == 10
        assert accounting["shared_execution_observations"] == 1
        assert accounting["outcomes"]["independently_accepted_goals"] is None


def test_same_title_or_process_run_does_not_attribute_unlinked_work(source):
    checkpoint = run(source)
    goal(source, checkpoint)
    publish(source, [checkpoint], [call(source, checkpoint, parent_run_id="", parent_source_id="")])
    accounting = summary(source)["goals"][0]["consumption"]
    assert accounting["usage"]["calls"] is None
    assert accounting["usage"]["total_tokens"] is None
    assert accounting["billing"]["comparable_total"] is None
    assert accounting["outcomes"]["completed_execution_observations"] is None


def test_bound_checkpoint_outside_lifecycle_tail_does_not_hide_explicit_child(source):
    checkpoint = run(source)
    goal(source, checkpoint)
    publish(source, [], [call(source, checkpoint)])
    accounting = summary(source)["goals"][0]["consumption"]
    assert accounting["usage"]["total_tokens"] == 10
    assert accounting["lineage_diagnostics"] == ["ancestor_unavailable_in_window"]
    assert accounting["lifetime_complete"] is False


def test_missing_and_corrupt_feed_makes_cost_partial_not_free(source):
    checkpoint = run(source)
    goal(source, checkpoint)
    publish(source, [checkpoint], [call(source, checkpoint), call(source, checkpoint, attempt=2, usage=None)])
    path = Path(source["data_dir"]) / "agents" / "ledger.jsonl"
    with path.open("a") as handle:
        handle.write("broken\n")
    accounting = summary(source)["goals"][0]["consumption"]
    assert accounting["coverage"] == "partial_window"
    assert accounting["usage"]["token_coverage"] == "partial"
    assert accounting["billing"]["missing_amount_observations"] == 1
    assert accounting["billing"]["comparable_total"] is None


def test_contract_revision_retains_cost_and_unverified_attestation_never_enables_ratio(source):
    checkpoint = run(source)
    value = goal(source, checkpoint)
    publish(source, [checkpoint], [call(source, checkpoint)])
    revised = dict(value["contract"], statement="changed statement")
    goals.define(source["source_id"], revised, "operator", "change", 2)
    evidence = Path(source["workspace"]) / "report"
    evidence.write_text("VERDICT: PASS - not an independently authenticated acceptance")
    goals.attest(value["goal_key"], "review", "accepted", str(evidence), "operator", "recorded", 3)
    current = summary(source)["goals"][0]
    assert current["acceptance"] == "operator_attested"
    assert current["consumption"]["usage"]["total_tokens"] == 10
    assert current["consumption"]["contract_scope"] == "all_retained_run_bindings_across_goal_revisions"
    assert current["efficiency"]["cost_per_accepted_goal"] is None


def test_stage_and_declared_role_bins_include_failures_not_semantic_phase_guesses(source):
    checkpoint = run(source)
    goal(source, checkpoint)
    publish(source, [checkpoint], [call(source, checkpoint, role="orchestrator", task_id="planner", status="failed"),
        call(source, checkpoint, role="worker"), call(source, checkpoint, phase="validation")])
    accounting = summary(source)["goals"][0]["consumption"]
    assert [row["role"] for row in accounting["stages"]] == ["orchestrator", "worker"]
    assert accounting["stages"][0]["outcomes"]["failed_execution_observations"] == 1
    assert accounting["stages"][1]["usage"]["calls"] == 1
    assert accounting["billing"]["provider_requests"]["observed_total"] is None


def test_cycles_are_not_used_to_assign_goal_cost(source):
    first, second = run(source), run(source, "b")
    first["parent_run_id"], first["parent_source_id"] = second["run_id"], second["source_id"]
    second["parent_run_id"], second["parent_source_id"] = first["run_id"], first["source_id"]
    goal(source, run(source))
    publish(source, [first, second], [call(source, second)])
    accounting = summary(source)["goals"][0]["consumption"]
    assert accounting["usage"]["calls"] is None
    assert accounting["unattributed_lineage_observations_in_scoped_sources"] == 1


def test_duplicate_feeds_complement_currency_but_conflicting_amounts_are_visible(source):
    checkpoint = run(source)
    goal(source, checkpoint)
    initial = call(source, checkpoint, usage={"totalTokens": 10, "cost_total": 1})
    publish(source, [checkpoint], [initial])
    event = {**initial, "event": "task_finish", "stage_id": "implement", "usage": {
        "total_tokens": 10, "cost": {"total": 1, "currency": "USD"}, "assistant_usage_messages": 2}}
    rows(source["events_file"], [event])
    accounting = summary(source)["goals"][0]["consumption"]
    assert accounting["usage"]["calls"] == 1
    assert accounting["billing"]["comparable_total"] == 1
    event["usage"]["cost"]["total"] = 2
    rows(source["events_file"], [event])
    view = summary(source)
    accounting = view["goals"][0]["consumption"]
    assert accounting["usage"]["cost_total"] is None
    assert accounting["billing"]["comparable_total"] is None
    assert any("conflicting duplicate usage" in note for note in view["diagnostics"])


def test_explicit_cross_registered_source_lineage_is_scoped_not_title_matched(source, tmp_path):
    checkpoint = run(source)
    goal(source, checkpoint)
    publish(source, [checkpoint], [])
    data = tmp_path / "other-data"
    data.mkdir()
    other = task_sources.source("other", str(data))
    task_sources.register_source(other)
    nested = run(other, "b", checkpoint["run_id"], checkpoint["source_id"])
    publish(other, [nested], [call(other, nested)])
    assert task_center.snapshot()["goals"][0]["consumption"]["usage"]["total_tokens"] == 10
    # A source-filtered query does not silently crawl the child source's files.
    assert summary(source)["goals"][0]["consumption"]["usage"]["total_tokens"] is None


def test_stage_bin_limit_preserves_whole_window_totals_and_reports_omission(source):
    checkpoint = run(source)
    goal(source, checkpoint)
    values = [call(source, checkpoint, task_id=f"worker{i}", role=f"role{i:03}") for i in range(130)]
    publish(source, [checkpoint], values)
    accounting = summary(source)["goals"][0]["consumption"]
    assert accounting["usage"]["total_tokens"] == 1300
    assert len(accounting["stages"]) == 128 and accounting["omitted_stage_bins"] == 2
    assert len(accounting["child_task_refs"]) == 32 and accounting["omitted_child_refs"] == 98
