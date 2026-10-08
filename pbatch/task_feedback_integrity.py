"""Explicit saved-copy byte verification only; never open originals or execute case text."""
from __future__ import annotations

from pathlib import Path

from .task_feedback_io import proof
from .task_feedback_schema import MAX_INPUT_BYTES, content_digest, decode, digest, validate_case


def validate_copy(value: dict, raw: bytes) -> str:
    """Bind complete bytes and closed case to recorded metadata; not authentication."""
    case = validate_case(decode(raw, MAX_INPUT_BYTES))
    content_hash = content_digest(case)
    if (proof(Path(value["input"]["path"]), raw) != value["input"] or
            digest(case) != value["case_sha256"] or case["case_id"] != value["case_id"] or
            (value["schema_version"] == 2 and content_hash != value["case_content_sha256"])):
        raise ValueError("feedback private copy changed; no existing evidence was overwritten")
    return content_hash


def verify_copy(key: str, *, confirm: bool = False) -> dict:
    if confirm is not True:
        raise ValueError("feedback copy verification needs explicit --confirm; no metadata or case was read")
    from . import task_feedback_store as store
    # Validate the typed key before touching the filesystem; query paths grant no access.
    store.feedback_path(key)
    value = store.load_feedback(key)
    raw, _ = store.read_bytes(store.feedback_root() / (key + ".case"), MAX_INPUT_BYTES, private=True)
    content_hash = validate_copy(value, raw)
    if store.load_feedback(key) != value:
        raise ValueError("feedback metadata changed during saved-copy verification; result withheld")
    return {"schema_version": 1, "method": "feedback-saved-copy-bytes-v1", "feedback_id": key,
        "metadata_schema_version": value["schema_version"], "version": value["version"], "state": value["state"],
        "verification": "recorded_copy_bytes_matching", "input_bytes": value["input"]["bytes"],
        "input_sha256": value["input"]["sha256"], "case_sha256": value["case_sha256"],
        "case_content_sha256": content_hash, "current_source_checked": False, "stores_receipt": False,
        "independent_reproduction": "not_performed", "root_cause": "unverified_hypothesis",
        "recoverable_archive_verified": False, "execution_authority": "not_granted",
        "cleanup_authority": "not_granted", "upload_authorized": False, "release_authorized": False,
        "basis": "bounded_complete_saved_bytes_matching_recorded_metadata_not_an_atomic_snapshot_or_authenticated_evidence"}
