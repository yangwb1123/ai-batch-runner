"""Goal records are explicit, versioned, evidence-bound, and not execution rights."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pbatch import task_center, task_goal_store as store, task_sources
from pbatch.task_goal_schema import validate_contract
from pbatch.task_goal_view import EvidenceWindow
from pbatch.task_lifecycle import source_identity
from pbatch.task_state_io import atomic_write, locked


@pytest.fixture
def source(tmp_path, monkeypatch):
    home = tmp_path / "runtime"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(home))
    data = home / "projects" / "demo"
    data.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    item = task_sources.source("demo", str(data), str(project))
    task_sources.register_source(item)
    return item


def contract():
    return {"schema_version": 1, "goal_id": "reliability", "statement": "同一个已确认的目标",
            "criteria": [{"id": "tests", "description": "复现与回归证据"},
                         {"id": "delivery", "description": "交付物审查证据"}]}


def define(source):
    return store.define(source["source_id"], contract(), "operator", "confirmed scope")


def checkpoint(source, run_id="pl-" + "a" * 32):
    value = {"schema_version": 1, "kind": "pipeline", "run_id": run_id,
             "source_id": source_identity(Path(source["data_dir"])), "name": "example",
             "status": "succeeded", "started_at": 100, "ts": "2026-10-10T00:00:10Z", "seq": 3,
             "plan_complete": True, "steps": [{"id": "implement", "dependencies": [],
                "state": "succeeded", "gate": False}], "stage": "implement",
             "parent_run_id": "", "parent_source_id": "", "process_run_id": "process"}
    path = Path(source["data_dir"]) / "tasks" / "runs" / (run_id + ".json")
    with locked(path):
        atomic_write(path, value)
    return value


def test_contract_needs_explicit_cli_confirmation_and_unknown_fields_fail_closed(source, tmp_path, capsys):
    from pbatch.ops import ops_main
    path = tmp_path / "goal.json"
    path.write_text(json.dumps(contract()))
    args = ["goals", "define", "--source", "demo", "--file", str(path),
            "--by", "operator", "--reason", "scope"]
    assert ops_main(args) == 2
    assert not (task_sources.catalog_path().parent / "goals").exists()
    assert ops_main([*args, "--confirm"]) == 0
    capsys.readouterr()
    bad = {**contract(), "auto_merge": True}
    with pytest.raises(ValueError, match="fields"):
        validate_contract(bad)


def test_read_only_goal_query_creates_no_runtime(tmp_path, monkeypatch):
    root = tmp_path / "absent"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(root))
    assert task_center.snapshot()["goals"] == []
    assert not root.exists()


def test_pipeline_success_and_verdict_text_do_not_accept_a_goal(source):
    value = define(source)
    run = checkpoint(source)
    store.bind(value["goal_key"], run["run_id"], "operator", "same goal", 1)
    evidence = Path(source["workspace"]) / "report.md"
    evidence.write_text("VERDICT: PASS - everything is great")
    goal = task_center.snapshot()["goals"][0]
    assert goal["acceptance"] == "pending"
    assert goal["progress"]["matching_attestations"] == 0
    assert goal["semantic_verification"] == "not_performed"
    assert goal["runs"][0]["checkpoint"]["liveness"] == "not_verified"
    assert goal["capabilities"]["control"] is False


def test_attestation_is_bound_to_complete_bytes_and_not_semantic_verification(source):
    value = define(source)
    path = Path(source["workspace"]) / "evidence.txt"
    path.write_text("first evidence")
    for index, criterion in enumerate(("tests", "delivery"), 1):
        store.attest(value["goal_key"], criterion, "accepted", str(path), "reviewer", "reviewed", index)
    goal = task_center.snapshot()["goals"][0]
    assert goal["acceptance"] == "operator_attested"
    assert goal["reviewer_identity"] == "not_authenticated"
    assert goal["efficiency"]["cost_per_accepted_goal"] is None
    assert goal["progress"]["goal_percent"] is None
    path.write_text("other evidence")  # same byte count, unlike a size-only receipt
    goal = task_center.snapshot()["goals"][0]
    assert goal["acceptance"] == "evidence_not_current"
    assert goal["criteria"][0]["evidence"]["status"] == "changed"


def test_revision_keeps_history_bindings_and_invalidates_old_attestations(source):
    value = define(source)
    run = checkpoint(source)
    store.bind(value["goal_key"], run["run_id"], "operator", "bound", 1)
    path = Path(source["workspace"]) / "report.md"
    path.write_text("review evidence")
    store.attest(value["goal_key"], "tests", "accepted", str(path), "reviewer", "accepted", 2)
    revised = contract()
    revised["statement"] = "调整后的目标，保留历史而不重置成本"
    value = store.define("demo", revised, "operator", "goal changed", expected=3)
    assert value["version"] == 4 and value["contract_revision"] == 2
    assert len(value["bindings"]) == 1 and len(value["attestations"]) == 1
    assert value["history"][0]["contract"]["statement"] == contract()["statement"]
    assert task_center.snapshot()["goals"][0]["criteria"][0]["state"] == "pending"


def test_stale_updates_and_corrupt_states_are_not_overwritten(source):
    value = define(source)
    path = store.goal_path(value["goal_key"])
    before = path.read_bytes()
    with pytest.raises(ValueError, match="revision conflict"):
        store.define("demo", contract(), "other", "overwrite", expected=0)
    assert path.read_bytes() == before
    path.write_text("broken")
    with pytest.raises(ValueError, match="invalid"):
        store.define("demo", contract(), "other", "repair", expected=1)
    assert path.read_text() == "broken"
    view = task_center.snapshot()
    assert view["goals"] == [] and any("goal metadata invalid" in note for note in view["diagnostics"])


@pytest.mark.parametrize("which", ["outside", "symlink", "missing", "empty"])
def test_unsafe_or_absent_evidence_does_not_make_an_acceptance_receipt(source, tmp_path, which):
    value = define(source)
    path = Path(source["workspace"]) / "evidence"
    outside = tmp_path / "outside"
    outside.write_text("private")
    if which == "outside":
        path = outside
    elif which == "symlink":
        path.symlink_to(outside)
    elif which == "empty":
        path.touch()
    before = store.goal_path(value["goal_key"]).read_bytes()
    with pytest.raises((ValueError, OSError)):
        store.attest(value["goal_key"], "tests", "accepted", str(path), "reviewer", "reviewed", 1)
    assert store.goal_path(value["goal_key"]).read_bytes() == before


def test_cross_source_run_and_unknown_run_cannot_be_bound(source, tmp_path):
    value = define(source)
    other = tmp_path / "other-runtime"
    other.mkdir()
    other_source = task_sources.source("other", str(other))
    task_sources.register_source(other_source)
    run = checkpoint(other_source)
    with pytest.raises(ValueError, match="not found"):
        store.bind(value["goal_key"], run["run_id"], "operator", "wrong project", 1)
    assert store.load_goal(value["goal_key"])["bindings"] == []


def test_goal_history_is_not_discarded_to_fit_a_budget(source, monkeypatch):
    value = define(source)
    monkeypatch.setattr(store, "MAX_HISTORY", 1)
    before = store.goal_path(value["goal_key"]).read_bytes()
    with pytest.raises(ValueError, match="history budget"):
        store.attest(value["goal_key"], "tests", "rejected", "", "reviewer", "reject", 1)
    assert store.goal_path(value["goal_key"]).read_bytes() == before


def test_matching_titles_in_different_projects_are_separate_goals(source, tmp_path):
    first = define(source)
    root = tmp_path / "other"
    root.mkdir()
    other = task_sources.source("other", str(root))
    task_sources.register_source(other)
    second = define(other)
    assert first["goal_key"] != second["goal_key"]
    assert len(task_center.snapshot()["goals"]) == 2


def test_goal_evidence_hashing_has_a_shared_query_budget(source):
    path = Path(source["workspace"]) / "proof"
    path.write_text("some proof")
    reference = store.evidence_reference(source, str(path))
    window = EvidenceWindow()
    window.remaining = 2
    assert window.verify(source, reference) == "unavailable"
    assert window.remaining == 2  # rejected from metadata, before reading content


def test_goal_state_schema_corruption_is_visible_and_not_repaired(source):
    value = define(source)
    altered = copy.deepcopy(value)
    altered["contract"]["statement"] = "modified without revision"
    path = store.goal_path(value["goal_key"])
    path.write_text(json.dumps(altered))
    view = task_center.snapshot()
    assert view["goals"] == []
    assert any("digest mismatch" in note for note in view["diagnostics"])
    assert json.loads(path.read_text()) == altered
