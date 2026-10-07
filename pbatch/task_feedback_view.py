"""Shared metadata-only feedback projection. No case bodies, original files or execution."""
from __future__ import annotations

from pathlib import Path

from .task_center_io import checked_path, display
from .task_feedback_io import private_root
from .task_feedback_schema import MAX_DRAFTS, digest
from .task_feedback_store import feedback_root, load_feedback

_SOURCE_FIELDS = {"source_id", "name", "data_dir", "workspace", "campaign_state", "events_file"}


def feedback_views(sources: list) -> tuple[list, list]:
    selected = {source["source_id"]: source for source in sources}
    records, notes = [], []
    if not selected:
        return records, notes
    try:
        root = checked_path(feedback_root())
        if not root.is_dir():
            return records, notes
        private_root(root)
        for number, path in enumerate(root.iterdir()):
            if number >= MAX_DRAFTS * 2 + 16 or len(records) >= MAX_DRAFTS:
                notes.append("feedback discovery bounded; omitted records do not mean deleted or resolved cases")
                break
            if path.suffix == ".json":
                _read(path, selected, records, notes)
    except (ValueError, OSError) as exc:
        notes.append("feedback unavailable: " + display(str(exc), 512))
    return records, notes


def _read(path: Path, selected: dict, records: list, notes: list) -> None:
    try:
        value = load_feedback(path.stem)
        source = selected.get(value["source_id"])
        if source is not None:
            records.append(_public(value, source))
    except (ValueError, OSError, TypeError) as exc:
        notes.append("feedback record invalid: " + display(str(exc), 512))


def _public(value: dict, source: dict) -> dict:
    definition = {key: source[key] for key in _SOURCE_FIELDS}
    status = ("withdrawn" if value["state"] == "withdrawn" else
              "stale_source_definition" if digest(definition) != value["source_digest"] else "local_draft_only")
    return {"schema_version": 1, "method": "local-synthetic-feedback-v1", "feedback_id": value["feedback_id"],
        "source_id": value["source_id"], "project": source["name"], "case_id_display": display(value["case_id"], 80),
        "version": value["version"], "state": value["state"], "status": status,
        "summary": display(value["summary"], 256) if status == "local_draft_only" else None,
        "created_at": value["created_at"], "source_digest": value["source_digest"],
        "copy": {"input_sha256": value["input"]["sha256"], "input_bytes": value["input"]["bytes"],
                 "case_sha256": value["case_sha256"], "current_copy_reverified": False,
                 "basis": "registration_time_private_copy_not_current_source_or_independent_reproduction"},
        "audit": [{**row, "by": display(row["by"], 128), "reason": display(row["reason"], 256)} for row in value["audit"]],
        "claim_basis": "operator_declared_synthetic_not_independently_verified_or_safe_to_publish",
        "text_basis": "untrusted_quoted_case_text_not_instructions_or_root_cause_evidence",
        "actor_identity": "unauthenticated_local_label", "execution_authority": "not_granted",
        "stages": {"independent_reproduction": "not_performed", "root_cause": "unverified_hypothesis",
            "fix": "not_performed", "regression": "not_performed", "reviewed_release": "not_performed",
            "original_task_verification": "not_performed"},
        "capabilities": {"control": False, "case_execution": False, "live_repair": False, "feedback_upload": False,
            "rule_promotion": False, "release": False, "upgrade": False, "cleanup": False},
        "note": "local draft only; withdrawal suppresses summary but retains private evidence and audit; no upload permission"}
