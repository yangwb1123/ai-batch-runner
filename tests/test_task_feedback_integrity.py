"""Saved-copy checks are explicit and ephemeral, never reproduction or original-source reads."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from test_task_feedback import source, feedback, drafting
from pbatch import task_feedback_store as store, task_feedback_io as io, task_sources
from pbatch.task_feedback_integrity import verify_copy
from pbatch.task_feedback_schema import MAX_INPUT_BYTES, content_digest, state_bytes, validate_state
from pbatch.task_feedback_view import feedback_views
from pbatch.ops import ops_main


def legacy(feedback):
    value = copy.deepcopy(feedback[2])
    value["schema_version"] = 1
    value.pop("case_content_sha256")
    store.feedback_path(value["feedback_id"]).write_bytes(state_bytes(value))
    return value


def test_new_metadata_content_identity_is_separate_from_raw_bytes_and_case_label(feedback):
    _, path, value = feedback
    assert value["schema_version"] == 2 and value["version"] == 1
    assert value["case_content_sha256"] == content_digest(json.loads(path.read_text()))
    assert value["case_content_sha256"] != value["case_sha256"]
    assert validate_state(value) == value


@pytest.mark.parametrize("schema", [True, 0, 3, "2", None])
def test_metadata_schema_refuses_unknown_versions_not_synthetic_case_schema(feedback, schema):
    value = copy.deepcopy(feedback[2]); value["schema_version"] = schema
    with pytest.raises(ValueError):
        validate_state(value)


@pytest.mark.parametrize("change", ["missing", "bad_digest", "legacy_extra", "unknown_field"])
def test_closed_v2_identity_cannot_be_optional_or_added_to_legacy_state(feedback, change):
    value = copy.deepcopy(feedback[2])
    if change == "missing":value.pop("case_content_sha256")
    elif change == "bad_digest":value["case_content_sha256"] = "unknown"
    elif change == "legacy_extra":value["schema_version"] = 1
    else:value["verify_passed"] = True
    with pytest.raises(ValueError):
        validate_state(value)


def test_verify_without_confirmation_never_reads_metadata_source_or_copy(monkeypatch):
    monkeypatch.setattr(store, "load_feedback", lambda *args: pytest.fail("metadata read"))
    monkeypatch.setattr(store, "read_bytes", lambda *args, **kwargs: pytest.fail("copy read"))
    with pytest.raises(ValueError, match="explicit --confirm"):
        verify_copy("fd-" + "a" * 32)
    assert not store.feedback_root().exists()


@pytest.mark.parametrize("key", ["../etc/passwd", "fd-" + "g" * 32, "pp-" + "a" * 32, "/tmp/case", True])
def test_typed_feedback_key_is_validated_before_any_read(monkeypatch, key):
    monkeypatch.setattr(store, "load_feedback", lambda *args: pytest.fail("invalid key read"))
    with pytest.raises(ValueError, match="invalid feedback identity"):
        verify_copy(key, confirm=True)


def test_verify_after_source_loss_only_reads_saved_evidence_and_writes_nothing(feedback, monkeypatch):
    source, path, value = feedback
    path.unlink()  # owned fixture only
    task_sources.unregister_source(source["source_id"])
    root = store.feedback_root()
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    original = store.read_bytes
    reads = []
    def saved_only(file, *args, **kwargs):
        assert root in Path(file).parents and file != path
        reads.append(Path(file).suffix)
        return original(file, *args, **kwargs)
    monkeypatch.setattr(store, "read_bytes", saved_only)
    monkeypatch.setattr(store, "_source", lambda *args: pytest.fail("origin source lookup"))
    monkeypatch.setattr(store, "_input_path", lambda *args: pytest.fail("origin lookup"))
    import subprocess, urllib.request
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("case execution"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("network access"))
    report = verify_copy(value["feedback_id"], confirm=True)
    assert reads == [".json", ".case", ".json"]
    assert report["verification"] == "recorded_copy_bytes_matching"
    assert report["case_content_sha256"] == value["case_content_sha256"]
    assert report["current_source_checked"] is report["stores_receipt"] is report["recoverable_archive_verified"] is False
    assert report["root_cause"] == "unverified_hypothesis" and report["independent_reproduction"] == "not_performed"
    assert report["execution_authority"] == report["cleanup_authority"] == "not_granted"
    assert report["upload_authorized"] is report["release_authorized"] is False
    assert "PRIVATE-CASE-BODY" not in json.dumps(report) and str(path) not in json.dumps(report)
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()} == before


def test_legacy_idempotence_verification_and_withdrawal_never_upgrade_metadata(feedback):
    value = legacy(feedback)
    path = store.feedback_path(value["feedback_id"])
    before = path.read_bytes()
    assert drafting(feedback[0], feedback[1]) == value and path.read_bytes() == before
    report = verify_copy(value["feedback_id"], confirm=True)
    assert report["metadata_schema_version"] == 1 and report["case_content_sha256"]
    assert path.read_bytes() == before
    row = feedback_views([feedback[0]])[0][0]
    assert row["copy"]["case_content_sha256"] is None
    assert row["duplicate_inspection"]["selection"] == "unavailable_legacy_content_identity"
    withdrawn = store.withdraw(value["feedback_id"], "operator", "preserve legacy history", 1)
    assert withdrawn["schema_version"] == 1 and "case_content_sha256" not in withdrawn
    assert verify_copy(value["feedback_id"], confirm=True)["state"] == "withdrawn"
    assert store.load_feedback(value["feedback_id"]) == withdrawn


@pytest.mark.parametrize("damage", ["missing", "changed", "same_size", "over_budget", "symlink", "hardlink", "fifo", "public_mode"])
def test_damaged_or_unsafe_copy_is_not_repaired_or_replaced(feedback, tmp_path, damage):
    _, _, value = feedback
    blob = store.feedback_root() / (value["feedback_id"] + ".case")
    raw = blob.read_bytes(); metadata = store.feedback_path(value["feedback_id"]).read_bytes()
    if damage in {"missing", "symlink", "fifo"}:blob.unlink()
    if damage == "changed":blob.write_bytes(b"damaged")
    elif damage == "same_size":blob.write_bytes(raw.replace(b"PRIVATE-CASE-BODY", b"CHANGED-CASE-BODY"))
    elif damage == "over_budget":blob.write_bytes(b" " * (MAX_INPUT_BYTES + 1))
    elif damage == "symlink":blob.symlink_to(feedback[1])
    elif damage == "hardlink":os.link(blob, tmp_path / "aliased-case")
    elif damage == "fifo":os.mkfifo(blob)
    elif damage == "public_mode":blob.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        verify_copy(value["feedback_id"], confirm=True)
    assert store.feedback_path(value["feedback_id"]).read_bytes() == metadata
    assert blob.is_symlink() if damage == "symlink" else True
    if damage == "same_size":assert blob.read_bytes() != raw and blob.stat().st_size == len(raw)


def test_valid_hex_content_identity_conflict_is_withheld_not_rewritten(feedback):
    value = copy.deepcopy(feedback[2]); value["case_content_sha256"] = "0" * 64
    path = store.feedback_path(value["feedback_id"]); path.write_bytes(state_bytes(value))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="copy changed"):
        verify_copy(value["feedback_id"], confirm=True)
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="cannot be rewritten"):
        drafting(feedback[0], feedback[1])


def test_withdrawal_during_verification_withholds_result_without_restoring_state(feedback, monkeypatch):
    value = feedback[2]; read = store.read_bytes
    def withdrawing(path, *args, **kwargs):
        result = read(path, *args, **kwargs)
        if Path(path).suffix == ".case":store.withdraw(value["feedback_id"], "operator", "withdraw during read", 1)
        return result
    monkeypatch.setattr(store, "read_bytes", withdrawing)
    with pytest.raises(ValueError, match="metadata changed"):
        verify_copy(value["feedback_id"], confirm=True)
    assert store.load_feedback(value["feedback_id"])["state"] == "withdrawn"


def test_copy_changes_during_fd_read_refuses_without_truncation(feedback, monkeypatch):
    value = feedback[2]; fstat = io.os.fstat
    blob = store.feedback_root() / (value["feedback_id"] + ".case")
    inode, calls = blob.stat().st_ino, []
    def unstable(fd):
        info = fstat(fd)
        if info.st_ino == inode:
            calls.append(fd)
            # Admission, before-read, after-read samples of this exact owned inode.
            if len(calls) == 3:
                blob.write_bytes(blob.read_bytes() + b" ")
                return fstat(fd)
        return info
    monkeypatch.setattr(io.os, "fstat", unstable)
    with pytest.raises(ValueError, match="changed during"):
        verify_copy(value["feedback_id"], confirm=True)


def test_cli_verify_requires_confirm_and_keeps_zero_narrowly_scoped(feedback, capsys):
    key = feedback[2]["feedback_id"]
    assert ops_main(["feedback", "verify-copy", key]) == 2
    assert "explicit --confirm" in capsys.readouterr().out
    assert ops_main(["feedback", "verify-copy", key, "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["independent_reproduction"] == "not_performed"
    assert ops_main(["feedback", "verify-copy", "../etc/passwd", "--confirm"]) == 2
    assert "invalid feedback identity" in capsys.readouterr().out
