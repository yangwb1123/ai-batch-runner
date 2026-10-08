"""Exact closed-content metadata matches in the selected bounded view, not semantic dedup."""
from __future__ import annotations

MAX_MATCH_REFS = 8


def attach_duplicates(records: list, *, incomplete: bool) -> None:
    """Pure projection: no case reads, clocks, writes, model calls or merge authority."""
    groups = {}
    untracked = sum(row["copy"].get("case_content_sha256") is None for row in records)
    for row in records:
        if _eligible(row):
            groups.setdefault(_key(row), []).append(row["feedback_id"])
    for row in records:
        peers = sorted(key for key in groups.get(_key(row), []) if key != row["feedback_id"]) if _eligible(row) else []
        row["duplicate_inspection"] = {
            "method": "exact-synthetic-content-metadata-v1", "selection": _selection(row, peers),
            "observed_matches": len(peers), "matching_feedback_ids": peers[:MAX_MATCH_REFS],
            "omitted_match_refs": max(0, len(peers) - MAX_MATCH_REFS),
            "scope": "same_full_source_id_and_definition_in_selected_bounded_metadata",
            "coverage": "incomplete_metadata_window" if incomplete else "bounded_observed_rows_only",
            "content_untracked_records": untracked, "current_copies_reverified": False,
            "semantic_comparison": "not_performed", "root_cause_equivalence": "not_verified",
            "merge_authority": "not_granted", "reuse_authority": "not_granted",
            "basis": "all_closed_case_fields_except_case_id_canonical_JSON_no_text_or_input_order_normalization",
            "note": "matches are inspection candidates, not duplicate incidents; zero matches is not proof of uniqueness"}


def _key(row: dict) -> tuple:
    return row["source_id"], row["source_digest"], row["copy"].get("case_content_sha256")


def _eligible(row: dict) -> bool:
    return row["status"] == "local_draft_only" and row["copy"].get("case_content_sha256") is not None


def _selection(row: dict, peers: list) -> str:
    if row["status"] != "local_draft_only":
        return "withheld_inactive_or_stale_source"
    if row["copy"].get("case_content_sha256") is None:
        return "unavailable_legacy_content_identity"
    return "matching_metadata_candidates" if peers else "no_other_matching_metadata_observed"
