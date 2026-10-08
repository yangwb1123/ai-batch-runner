"""Bounded attention facts are quoted observations, never causes, grants or hidden reads."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pbatch import task_attention as attention, task_center
from pbatch.task_center_terminal_view import records
from test_task_center import source, ledger, agent_row


def scope(number=1):
    return {"source_id": "src-" + f"{number:024x}", "name": "token=private-label", "coverage": []}


def empty(sources=None):
    return {"sources": [scope()] if sources is None else sources, "diagnostics": [],
            **{kind: [] for kind in ("tasks", "goals", "intents", "repairs", "resources", "feedback")}}


def candidate(state="failed", number=1, source=None, partial=False):
    row = {"status": state, "record_ref": "quoted-ref", "phase": "execution"}
    return (number, f"{number:032x}", source or scope(), [row], partial)


@pytest.mark.parametrize("state,reason", [
    ("failed", "reported_failure"), ("rejected", "reported_rejection"),
    ("PIPELINE_FAILED", "reported_pipeline_failure"), ("GATE_REJECTED", "reported_gate_rejection"),
    ("validation_failed", "reported_validation_failure"), ("MERGE_BLOCKED", "reported_merge_block"),
    ("BUDGET_BLOCKED", "reported_budget_block"), ("PROVIDER_BREAKER_PAUSED", "reported_provider_pause"),
    ("RECOVERY_BLOCKED", "reported_recovery_block"),
])
def test_exact_reported_state_is_a_fact_not_a_cause_or_permission(state, reason):
    value = attention.attention_view(empty(), [candidate(state)])
    item = value["items"][0]
    assert item["reason_code"] == reason and item["evidence"]["reported_status"] == state
    assert item["source_id"] == scope()["source_id"]
    assert item["target"] == {"kind": "tasks", "record_id": "tc-" + f"{1:032x}"}
    assert item["root_cause"] == "not_verified" and item["operator_action"] == "not_selected_or_authorized"
    assert not any(item["capabilities"].values()) and value["grants_execution"] is False


@pytest.mark.parametrize("state", ["succeeded", "RUNNING", "planned", "cancelled", "interrupted", "unknown", "succeeded FAIL retry now"])
def test_silence_activity_intentional_stop_unknown_or_quoted_text_do_not_invent_a_fault(state):
    assert attention.attention_view(empty(), [candidate(state)])["observed_items"] == 0


def test_pure_projection_does_not_read_write_probe_spawn_get_clock_or_rediscover(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("attention performed I/O, a clock read or discovery")
    view, candidates = empty(), [candidate(partial=True)]
    before = copy.deepcopy((view, candidates))
    import time, subprocess, urllib.request
    from pbatch import task_sources
    for name in ("stat", "read_bytes", "read_text", "write_bytes", "write_text", "iterdir"):
        monkeypatch.setattr(Path, name, forbidden)
    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(task_sources, "discover_sources", forbidden)
    value = attention.attention_view(view, candidates)
    assert value["items"][0]["evidence"]["partial_window"] is True
    assert (view, candidates) == before
    assert "private-label" not in json.dumps(value)


def test_counts_cover_all_candidates_but_only_bounded_stable_full_ids_are_retained():
    candidates = [candidate(number=i) for i in range(1, 302)]
    value = attention.attention_view(empty(), candidates)
    assert value["observed_items"] == 301 and len(value["items"]) == attention.MAX_ITEMS
    assert value["omitted_items"] == 301 - attention.MAX_ITEMS
    assert value["counts_by_reason"] == {"reported_failure": 301}
    assert value["counts_by_kind"] == {"tasks": 301}
    assert value == attention.attention_view(empty(), candidates[::-1])
    assert len({item["attention_id"] for item in value["items"]}) == attention.MAX_ITEMS


def test_identity_never_uses_title_redaction_display_time_or_filter_order():
    first, second = scope(1), scope(2)
    first["name"], second["name"] = "same title", "same title"
    value = attention.attention_view(empty([first, second]), [candidate(source=first), candidate(source=second)])
    assert len({item["attention_id"] for item in value["items"]}) == 2
    first["name"] = "new display title"
    changed = attention.attention_view(empty([first, second]), [candidate(source=second), candidate(source=first)])
    assert [item["attention_id"] for item in changed["items"]] == [item["attention_id"] for item in value["items"]]


def test_source_feed_gaps_do_not_treat_missing_optional_feeds_as_failure():
    source = scope()
    source["coverage"] = [{"feed": "optional-absent", "available": False, "truncated": False, "incomplete": False,
                            "malformed": 0, "error": None}]
    assert attention.attention_view(empty([source]), [])["observed_items"] == 0
    source["coverage"] += [{"feed": "known-gap-" + str(i), "truncated": True, "incomplete": False,
                            "malformed": 2, "error": None} for i in range(12)]
    item = attention.attention_view(empty([source]), [])["items"][0]
    assert item["target"] == {"kind": "sources", "record_id": source["source_id"]}
    assert len(item["evidence"]["feeds"]) == 8 and item["evidence"]["omitted_feed_refs"] == 4
    assert item["evidence"]["live_process_liveness"] == "not_verified"


@pytest.mark.parametrize("kind,field,state,code", [
    ("goals", "acceptance", "operator_rejected", "goal_record_rejected"),
    ("goals", "acceptance", "evidence_not_current", "goal_evidence_not_current"),
    ("intents", "status", "stale_contract", "intent_stale_contract"),
    ("intents", "status", "goal_unavailable", "intent_goal_unavailable"),
    ("repairs", "status", "expired_declaration", "repair_declaration_expired"),
    ("resources", "policy", "review_due_preserve", "resource_review_due"),
    ("feedback", "status", "stale_source_definition", "feedback_stale_source"),
])
def test_only_declared_supported_metadata_states_emit_fixed_body_free_cues(kind, field, state, code):
    view = empty()
    id_field = attention._METADATA[kind][0]
    value = {**scope(), id_field: "full-record-id", field: state, "version": 2,
             "summary": "PRIVATE-OLD-SUMMARY", "analysis": {"text": "PRIVATE-HISTORY"}, "expected": "PRIVATE-BODY"}
    view[kind] = [value]
    item = attention.attention_view(view, [])["items"][0]
    assert item["reason_code"] == code and item["evidence"]["record_version"] == 2
    assert "PRIVATE" not in json.dumps(item)
    value[field] = "withdrawn" if kind in {"feedback", "repairs"} else "revoked"
    assert attention.attention_view(view, [])["observed_items"] == 0


def test_pending_acceptance_unknown_billing_legacy_copy_and_duplicate_candidates_are_not_reported_failures():
    view = empty()
    for kind, (field, state, _) in attention._METADATA.items():
        view[kind] = [{**scope(), field: "id", state: "pending", "version": 1, "cost": None,
                       "duplicate_inspection": {"observed_matches": 4}, "content_identity": None}]
    result = attention.attention_view(view, [])
    assert result["observed_items"] == 0 and result["zero_items_proves_safety_or_no_missing_work"] is False


def test_snapshot_task_limit_does_not_change_attention_coverage_or_reopen_omitted_artifacts(source, monkeypatch):
    ledger(source, [agent_row(task_id="one", status="failed", output="not-to-read"),
                    agent_row(task_id="two", status="failed"), agent_row(task_id="three", status="succeeded", ts="2026-10-10T00:01:00Z")])
    narrow = task_center.snapshot(limit=1, source_id=source["source_id"], now=1800000000)
    broad = task_center.snapshot(limit=200, source_id=source["source_id"], now=1800000000)
    assert narrow["attention"] == broad["attention"]
    assert narrow["attention"]["observed_items"] == 2 and narrow["attention"]["coverage"]["task_candidates"] == 3
    assert len(narrow["tasks"]) == 1
    assert all(item["target"]["record_id"] not in {row["task_id"] for row in narrow["tasks"]}
               for item in narrow["attention"]["items"])
    assert len([key for key in records(narrow) if key.startswith("attention:")]) == 2


def test_recovered_latest_status_clears_failure_cue_not_the_retained_failure_history(source):
    ledger(source, [agent_row(status="failed"), agent_row(status="succeeded", attempt=2, ts="2026-10-10T00:00:30Z")])
    view = task_center.snapshot(source_id=source["source_id"])
    assert view["tasks"][0]["analysis"]["failure_observations"] == 1
    assert view["attention"]["observed_items"] == 0


def test_unavailable_catalog_and_unknown_source_are_visible_gaps_not_complete_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "empty"))
    view = task_center.snapshot(source_id="unregistered")
    assert view["attention"]["coverage"]["snapshot_diagnostics"] == 1
    assert view["attention"]["observed_items"] == 0
    assert not (tmp_path / "empty").exists()
    monkeypatch.setattr(task_center, "discover_sources", lambda: (_ for _ in ()).throw(ValueError("catalog unavailable")))
    view = task_center.snapshot()
    assert view["attention"]["coverage"]["snapshot_diagnostics"] == 1
    assert view["attention"]["zero_items_proves_safety_or_no_missing_work"] is False
