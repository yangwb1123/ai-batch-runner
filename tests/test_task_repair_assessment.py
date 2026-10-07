"""Admitted metadata can refute readiness, never prove authorization or liveness."""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from test_task_repair_preview import _checkpoint, _publish, _write
from test_task_repair_requests import source, prepared, register, view_of
from pbatch import task_center, task_goal_store, task_repair_store, task_sources
from pbatch.ops import ops_main
from pbatch.task_consumption import ConsumptionIndex
from pbatch.task_repair_assessment import assess_request
from pbatch.task_repair_preview import REQUIRED_CHECKS
from pbatch.task_repair_schema import encoded


def _checks(report):
    return {row["id"]: row["state"] for row in report["metadata_checks"]}


def _no_authority(report):
    assert report["ready_to_execute"] is False
    assert report["grants_execution"] is False and report["live_preflight_performed"] is False
    assert report["original_deadline"] is None and report["remaining_original_budget"] is None
    assert [row["id"] for row in report["required_execution_checks"]] == list(REQUIRED_CHECKS)
    assert all(row["state"] == "required_not_evaluated" for row in report["required_execution_checks"])
    assert set(REQUIRED_CHECKS).issubset(report["blockers"])
    json.dumps(report, allow_nan=False)


def _facts(prepared):
    value = register(prepared)
    snapshot = task_center.snapshot(source_id=prepared[0]["source_id"])
    goal = snapshot["goals"][0]
    lineage = snapshot["repairs"][0]["assessment"]["child_observations"]
    return value, goal, (prepared[2], False), lineage


def _child(source, run, **updates):
    return {"run_id": "child", "task_id": "task", "status": "running", "phase": "execution", "attempt": 1,
            "ts": "2026-10-10T00:00:00Z", "parent_run_id": run, "parent_source_id": source["source_id"], **updates}


def test_every_matching_metadata_check_still_leaves_all_execution_checks_unevaluated(prepared):
    value = register(prepared)
    report = view_of(value, prepared[0])["assessment"]
    assert report["decision"] == "blocked"
    assert set(_checks(report).values()) == {"observed_match"}
    assert report["blockers"] == list(REQUIRED_CHECKS)
    assert report["candidate_stage"]["stage_key"] == value["manifest"]["stage_key"]
    assert report["candidate_stage"]["prerequisite_reuse"] == "not_approved_even_if_previously_succeeded"
    assert report["remaining_declared_gates"] == 2
    _no_authority(report)


@pytest.mark.parametrize("mode,code", [("withdrawn", "request_withdrawn"), ("expired", "declared_deadline_expired")])
def test_inactive_requests_never_keep_a_candidate(prepared, mode, code):
    value = register(prepared)
    kwargs = {}
    if mode == "withdrawn":
        task_repair_store.withdraw(value["repair_id"], "operator", "stop request", 1)
    else:
        kwargs["now"] = 2_000_000_000
    report = view_of(value, prepared[0], **kwargs)["assessment"]
    assert report["decision"] == "inactive" and report["candidate_stage"] is None
    assert code in report["blockers"]
    _no_authority(report)


@pytest.mark.parametrize("damage", ["missing", "schema", "identity", "pin", "partial", "cancelled", "ambiguous", "incomplete"])
def test_unavailable_conflicting_or_ambiguous_checkpoint_withholds_candidate(prepared, damage):
    value, goal, observed, lineage = _facts(prepared)
    checkpoint = copy.deepcopy(observed[0])
    if damage == "missing":
        observed = None
    else:
        if damage == "schema":
            checkpoint["seq"] = True
        elif damage == "identity":
            checkpoint["source_id"] = "src-" + "0" * 24
        elif damage == "pin":
            checkpoint["name"] = "different fields at same sequence"
        elif damage == "cancelled":
            checkpoint["status"] = "cancelled"
        elif damage == "ambiguous":
            checkpoint["steps"][2].update(state="failed", dependencies=[])
        elif damage == "incomplete":
            checkpoint["plan_complete"] = False
        observed = (checkpoint, damage == "partial")
    report = assess_request(value, goal, observed, lineage, 1_700_000_000)
    assert report["decision"] == "blocked" and report["candidate_stage"] is None
    assert len(report["blockers"]) > len(REQUIRED_CHECKS)
    _no_authority(report)


@pytest.mark.parametrize("change,check", [("revision", "current_goal_revision"), ("source", "current_goal_revision"),
    ("binding", "retained_goal_run_binding"), ("header", "existing_goal_checkpoint_header"),
    ("missing_header", "existing_goal_checkpoint_header"), ("missing_goal", "current_goal_revision")])
def test_goal_or_known_header_conflict_is_not_a_recovery_grant(prepared, change, check):
    value, goal, observed, lineage = _facts(prepared)
    goal = copy.deepcopy(goal)
    if change == "revision":
        goal["version"] += 1
    elif change == "source":
        goal["source_id"] = "src-" + "0" * 24
    elif change == "binding":
        goal["runs"] = []
    elif change == "header":
        goal["runs"][0]["checkpoint"]["sequence"] += 1
    elif change == "missing_header":
        goal["runs"][0]["checkpoint"] = None
    else:
        goal = None
    report = assess_request(value, goal, observed, lineage, 1_700_000_000)
    assert _checks(report)[check] in {"unavailable", "observed_conflict"}
    assert report["candidate_stage"] is None
    _no_authority(report)


@pytest.mark.parametrize("field,new", [("plan_sha256", "0" * 64), ("declared_gate", True)])
def test_receipt_plan_and_gate_are_checked_not_treated_as_current_proof(prepared, field, new):
    value, goal, observed, lineage = _facts(prepared)
    value["receipt"][field] = new
    report = assess_request(value, goal, observed, lineage, 1_700_000_000)
    assert _checks(report)["registered_binding_receipt"] == "observed_conflict"
    assert report["candidate_stage"] is None
    _no_authority(report)


def test_return_limit_does_not_erase_child_warning_or_hide_original_request(prepared):
    source, _, checkpoint, _ = prepared
    value = register(prepared)
    rows = [_child(source, checkpoint["run_id"], task_id=str(number)) for number in range(12)]
    _write(Path(source["data_dir"]) / "agents/ledger.jsonl", rows)
    newer = _checkpoint(source, run_id="pl-" + "b" * 32, ts="2026-10-10T00:00:20Z")
    _write(Path(source["data_dir"]) / "tasks/lifecycle.jsonl", [checkpoint, newer])
    one = task_center.snapshot(1, source["source_id"])
    all_rows = task_center.snapshot(200, source["source_id"])
    assert one["tasks"][0]["run_id"] != value["manifest"]["run_id"]
    assert one["repairs"] == all_rows["repairs"]
    report = one["repairs"][0]["assessment"]
    assert "active_child_execution_observations" in report["blockers"]
    child = report["child_observations"]
    assert child["active_execution_observations"] == child["observed_child_tasks"] == 12
    assert len(child["child_task_refs"]) == 8 and child["omitted_child_refs"] == 4
    assert report["candidate_stage"] is None
    _no_authority(report)


@pytest.mark.parametrize("mode", ["untracked", "missing_ancestor", "cycle"])
def test_unresolved_lineage_is_disclosed_without_allocating_it_to_this_request(prepared, mode):
    source, _, checkpoint, _ = prepared
    value = register(prepared)
    run = "pl-" + "b" * 32
    parent = _checkpoint(source, run_id=run, parent_run_id=run, parent_source_id=source["source_id"])
    if mode == "cycle":
        _write(Path(source["data_dir"]) / "tasks/lifecycle.jsonl", [checkpoint, parent])
    child = _child(source, "" if mode == "untracked" else run)
    _write(Path(source["data_dir"]) / "agents/ledger.jsonl", [child])
    report = view_of(value, source)["assessment"]
    assert "ambiguous_or_unattributed_child_lineage" in report["blockers"]
    assert report["child_observations"]["unresolved_lineage_observations_in_scoped_sources"] == 1
    assert report["child_observations"]["active_execution_observations"] == 0
    assert report["child_observations"]["child_task_refs"] == []
    _no_authority(report)


def test_zero_active_rows_is_never_verified_liveness_or_remaining_budget(prepared):
    value = register(prepared)
    report = view_of(value, prepared[0])["assessment"]
    assert report["child_observations"]["active_execution_observations"] == 0
    assert report["child_observations"]["liveness"] == "not_verified"
    assert "no_active_row_is_not_proof_of_idle" in next(row["basis"] for row in report["metadata_checks"]
                                                       if row["id"] == "child_execution_observations")
    _no_authority(report)


def test_cross_source_identity_and_filters_never_invent_or_crawl_child_links(prepared):
    root, _, checkpoint, _ = prepared
    value = register(prepared)
    directory = Path(root["data_dir"]).parent / "other"
    directory.mkdir()
    other = task_sources.source("other", str(directory))
    task_sources.register_source(other)
    _publish(other, _checkpoint(other))  # same run/title, distinct source identity
    row = _child(other, checkpoint["run_id"])
    _write(directory / "agents/ledger.jsonl", [row])
    global_view = task_center.snapshot()
    report = next(row["assessment"] for row in global_view["repairs"] if row["repair_id"] == value["repair_id"])
    assert report["child_observations"]["observed_child_tasks"] == 0
    row["parent_source_id"] = root["source_id"]  # only explicit ancestry changes attribution
    _write(directory / "agents/ledger.jsonl", [row])
    global_view = task_center.snapshot()
    report = next(row["assessment"] for row in global_view["repairs"] if row["repair_id"] == value["repair_id"])
    assert report["child_observations"]["observed_child_tasks"] == 1
    assert "active_child_execution_observations" in report["blockers"]
    filtered = view_of(value, root)["assessment"]
    assert filtered["child_observations"]["observed_child_tasks"] == 0
    _no_authority(filtered)


def test_inspection_preserves_gate_total_even_when_later_steps_are_omitted(prepared):
    root, _, checkpoint, manifest = prepared
    previous = "acceptance"
    for number in range(20):
        name = f"gate-{number}"
        checkpoint["steps"].append({"id": name, "state": "pending", "gate": True, "dependencies": [previous]})
        previous = name
    _publish(root, checkpoint)
    from test_task_repair_requests import manifest_for
    prepared[3].update(manifest_for(root, prepared[1], task_center.snapshot()["tasks"][0]))
    value = register(prepared)
    report = view_of(value, root)["assessment"]
    assert report["remaining_declared_gates"] == 22
    _no_authority(report)


def test_inspection_reuses_one_admitted_consumption_index(prepared, monkeypatch):
    value = register(prepared)
    calls = []
    original = ConsumptionIndex.__init__
    def constructing(self, candidates, sources):
        calls.append(True)
        original(self, candidates, sources)
    monkeypatch.setattr(ConsumptionIndex, "__init__", constructing)
    view_of(value, prepared[0])
    assert len(calls) == 1


def test_operator_attestation_and_pass_prose_do_not_clear_execution_or_acceptance_checks(prepared):
    source, goal, _, _ = prepared
    artifact = Path(source["workspace"]) / "pass.md"
    artifact.write_text("VERDICT: PASS - claim only")
    goal = task_goal_store.attest(goal["goal_key"], "tests", "accepted", str(artifact), "operator", "claim", 2)
    prepared[3]["goal_version"] = goal["version"]
    value = register(prepared)
    public = task_center.snapshot(source_id=source["source_id"])
    assert public["goals"][0]["acceptance"] == "operator_attested"
    _no_authority(public["repairs"][0]["assessment"])
    assert public["repairs"][0]["repair_id"] == value["repair_id"]


def test_pure_inspection_has_no_io_mutation_or_clock_reads(prepared, monkeypatch):
    value, goal, observed, lineage = _facts(prepared)
    inputs = encoded({"request": value, "goal": goal, "checkpoint": observed, "lineage": lineage})
    def forbidden(*args, **kwargs):
        pytest.fail("pure inspection cannot open files or execute anything")
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    report = assess_request(value, goal, observed, lineage, 1_700_000_000)
    assert inputs == encoded({"request": value, "goal": goal, "checkpoint": observed, "lineage": lineage})
    _no_authority(report)


def test_cli_check_shares_report_and_never_returns_executor_success(prepared, capsys, monkeypatch):
    value = register(prepared)
    expected = view_of(value, prepared[0])["assessment"]
    before = task_repair_store.request_path(value["repair_id"]).read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("CLI inspection cannot call a provider or executor")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert ops_main(["repairs", "check", value["repair_id"], "--json"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result == {"repair_id": value["repair_id"], "assessment": expected}
    assert ops_main(["repairs", "check", value["repair_id"], "--source", prepared[0]["name"]]) == 3
    assert "不可执行；非实时预检" in capsys.readouterr().out
    assert ops_main(["repairs", "check", value["repair_id"], "--source", "/arbitrary/path"]) == 1
    capsys.readouterr()
    assert ops_main(["repairs", "check", "rq-" + "0" * 32]) == 1
    capsys.readouterr()
    assert task_repair_store.request_path(value["repair_id"]).read_bytes() == before
