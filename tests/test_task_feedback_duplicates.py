"""Content matches are scoped metadata candidates, not incident/semantic equivalence or reuse."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from test_task_feedback import source, feedback, case, input_file, drafting
from test_task_feedback_integrity import legacy
from pbatch import task_center, task_sources, task_feedback_store as store
from pbatch.task_feedback_duplicates import attach_duplicates, MAX_MATCH_REFS
from pbatch.task_feedback_integrity import verify_copy
from pbatch.task_feedback_schema import MAX_DRAFTS, content_digest
from pbatch.task_feedback_view import feedback_views


def test_all_content_but_case_label_is_bound_without_normalizing_text_or_fragment_order():
    value = case()
    assert content_digest(value) == content_digest(case("different-label"))
    assert content_digest(value) == content_digest(dict(reversed(list(value.items()))))
    for key in ("summary", "expected", "observed", "hypothesis"):
        changed = copy.deepcopy(value); changed[key] += " "
        assert content_digest(value) != content_digest(changed)
    changed = copy.deepcopy(value); changed["inputs"][0]["content"] += " "
    assert content_digest(value) != content_digest(changed)
    first = case(inputs=[{"name": "a", "content": "x"}, {"name": "b", "content": "y"}])
    second = copy.deepcopy(first); second["inputs"].reverse()
    assert content_digest(first) != content_digest(second)
    assert content_digest(case(summary="é")) != content_digest(case(summary="e\u0301"))


def test_identical_content_under_different_labels_remains_two_immutable_drafts(source):
    first = drafting(source, input_file(source, case("one"), "one.json"))
    second = drafting(source, input_file(source, case("two"), "two.json"))
    assert first["feedback_id"] != second["feedback_id"] and first["case_sha256"] != second["case_sha256"]
    assert first["case_content_sha256"] == second["case_content_sha256"]
    root = store.feedback_root(); before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    rows, _ = feedback_views([source]); by_id = {row["feedback_id"]: row for row in rows}
    hint = by_id[first["feedback_id"]]["duplicate_inspection"]
    assert hint["selection"] == "matching_metadata_candidates"
    assert hint["observed_matches"] == 1 and hint["matching_feedback_ids"] == [second["feedback_id"]]
    assert hint["current_copies_reverified"] is False
    assert hint["semantic_comparison"] == "not_performed" and hint["root_cause_equivalence"] == "not_verified"
    assert hint["merge_authority"] == hint["reuse_authority"] == "not_granted"
    assert task_center.snapshot(source_id=source["source_id"], limit=1)["feedback"] == rows
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before


def test_same_redacted_summary_does_not_group_different_full_content(source):
    first = drafting(source, input_file(source, case("one", summary="token=secret_one")))
    second = drafting(source, input_file(source, case("two", summary="token=secret_two")))
    rows, _ = feedback_views([source])
    assert rows[0]["summary"] == rows[1]["summary"] and first["case_content_sha256"] != second["case_content_sha256"]
    assert all(row["duplicate_inspection"]["selection"] == "no_other_matching_metadata_observed" for row in rows)
    assert "secret_one" not in json.dumps(rows) and "secret_two" not in json.dumps(rows)


def test_cross_source_matches_are_never_inferred_or_exposed_to_filtered_views(source, tmp_path):
    other_data = tmp_path / "other-data"; other_data.mkdir()
    other_work = tmp_path / "other-work"; other_work.mkdir()
    other = task_sources.source("other", str(other_data), str(other_work))
    task_sources.register_source(other)
    drafting(source, input_file(source, case("same-label")))
    second = drafting(other, input_file(other, case("same-label")))
    rows, _ = feedback_views([source, other])
    assert len(rows) == 2 and all(row["duplicate_inspection"]["observed_matches"] == 0 for row in rows)
    filtered = task_center.snapshot(source_id=source["source_id"])["feedback"]
    assert len(filtered) == 1 and second["feedback_id"] not in json.dumps(filtered)


def test_stale_definition_and_withdrawal_remove_matches_without_discarding_audit(source):
    first = drafting(source, input_file(source, case("one")))
    second = drafting(source, input_file(source, case("two")))
    store.withdraw(second["feedback_id"], "operator", "exclude withdrawn draft from hints", 1)
    rows, _ = feedback_views([source])
    assert all(row["duplicate_inspection"]["observed_matches"] == 0 for row in rows)
    inactive = next(row for row in rows if row["feedback_id"] == second["feedback_id"])
    assert inactive["duplicate_inspection"]["selection"] == "withheld_inactive_or_stale_source"
    task_sources.unregister_source(source["source_id"])
    changed = task_sources.source("changed definition", source["data_dir"], source["workspace"])
    task_sources.register_source(changed)
    rows, _ = feedback_views([changed])
    assert all(row["duplicate_inspection"]["selection"] == "withheld_inactive_or_stale_source" for row in rows)
    assert next(row for row in rows if row["feedback_id"] == first["feedback_id"])["audit"][0] == first["audit"][0]


def test_legacy_matches_are_unknown_even_after_explicit_ephemeral_copy_verification(feedback):
    source, path, _ = feedback
    value = legacy(feedback)
    drafting(source, input_file(source, case("same-content-new-id"), "second.json"))
    before = store.feedback_path(value["feedback_id"]).read_bytes()
    verify_copy(value["feedback_id"], confirm=True)
    rows, _ = feedback_views([source])
    assert all(row["duplicate_inspection"]["observed_matches"] == 0 for row in rows)
    assert all(row["duplicate_inspection"]["content_untracked_records"] == 1 for row in rows)
    assert store.feedback_path(value["feedback_id"]).read_bytes() == before


def test_query_uses_recorded_metadata_even_if_private_payload_missing_never_calls_verifier(source, monkeypatch):
    values = [drafting(source, input_file(source, case(name))) for name in ("one", "two")]
    (store.feedback_root() / (values[1]["feedback_id"] + ".case")).unlink()  # owned copy only
    read = store.read_bytes
    def metadata_only(path, *args, **kwargs):
        assert Path(path).suffix == ".json"
        return read(path, *args, **kwargs)
    monkeypatch.setattr(store, "read_bytes", metadata_only)
    monkeypatch.setattr(store, "_verify_case", lambda *args: pytest.fail("implicit payload verification"))
    rows, _ = feedback_views([source])
    assert all(row["duplicate_inspection"]["observed_matches"] == 1 for row in rows)
    assert all(row["duplicate_inspection"]["current_copies_reverified"] is False for row in rows)


def test_full_registry_match_refs_are_sorted_bounded_and_counts_precede_display_trim(source):
    values = [drafting(source, input_file(source, case("label-" + str(number)))) for number in range(MAX_DRAFTS)]
    rows, _ = feedback_views([source])
    assert len(rows) == MAX_DRAFTS
    hint = rows[0]["duplicate_inspection"]
    assert hint["observed_matches"] == MAX_DRAFTS - 1
    assert len(hint["matching_feedback_ids"]) == MAX_MATCH_REFS
    assert hint["omitted_match_refs"] == MAX_DRAFTS - 1 - MAX_MATCH_REFS
    assert hint["matching_feedback_ids"] == sorted(hint["matching_feedback_ids"])
    assert all(key in {item["feedback_id"] for item in values} for key in hint["matching_feedback_ids"])
    assert "not proof of uniqueness" in hint["note"]


def test_corrupt_or_bounded_window_never_becomes_a_complete_uniqueness_claim(feedback):
    source, _, _ = feedback
    (store.feedback_root() / ("fd-" + "0" * 32 + ".json")).write_bytes(b"corrupt metadata")
    rows, notes = feedback_views([source])
    assert notes and rows[0]["duplicate_inspection"]["coverage"] == "incomplete_metadata_window"
    before = copy.deepcopy(rows)
    attach_duplicates(rows, incomplete=True)
    assert rows == before
