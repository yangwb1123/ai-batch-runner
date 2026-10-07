"""Local synthetic drafts retain evidence and audit, never execute, upload or certify fixes."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from test_task_repair_preview import source
from pbatch import task_center, task_sources, task_feedback_store as store, task_feedback_io as io
from pbatch.task_feedback_schema import MAX_INPUT_BYTES, MAX_DRAFTS, validate_state, state_bytes
from pbatch.task_feedback_view import feedback_views
from pbatch.task_state_io import locked
from pbatch.ops import ops_main


def case(case_id="synthetic-bounds", **updates):
    return {"schema_version": 1, "case_id": case_id, "case_type": "operator_declared_synthetic",
        "summary": "Small quoted boundary example", "inputs": [{"name": "input", "content": "PRIVATE-CASE-BODY"}],
        "expected": "PRIVATE-EXPECTED-RESULT", "observed": "PRIVATE-OBSERVED-CLAIM",
        "hypothesis": "PRIVATE-UNVERIFIED-HYPOTHESIS", **updates}


def input_file(source, value=None, name="synthetic-case.json"):
    path = Path(source["workspace"]) / name
    path.write_text(json.dumps(value if value is not None else case(), ensure_ascii=False), encoding="utf-8")
    return path


def drafting(source, file=None, **options):
    file = file if file is not None else input_file(source)
    return store.draft(source["source_id"], str(file), "operator", "private synthetic case", confirm=True, **options)


@pytest.fixture
def feedback(source):
    path = input_file(source)
    return source, path, drafting(source, path)


def test_explicit_case_copy_is_private_complete_and_separate_from_projection(feedback):
    source, path, value = feedback
    root = store.feedback_root()
    blob = root / (value["feedback_id"] + ".case")
    assert blob.read_bytes() == path.read_bytes()
    assert value["input"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert root.stat().st_mode & 0o777 == 0o700
    assert blob.stat().st_mode & 0o777 == store.feedback_path(value["feedback_id"]).stat().st_mode & 0o777 == 0o600
    assert blob.stat().st_nlink == 1
    row = feedback_views([source])[0][0]
    assert row == task_center.snapshot(source_id=source["source_id"])["feedback"][0]
    text = json.dumps(row)
    for private in (str(path), "PRIVATE-CASE-BODY", "PRIVATE-EXPECTED-RESULT", "PRIVATE-OBSERVED-CLAIM", "PRIVATE-UNVERIFIED-HYPOTHESIS"):
        assert private not in text
    assert row["status"] == "local_draft_only" and row["execution_authority"] == "not_granted"
    assert all(item is False for item in row["capabilities"].values())
    assert row["stages"]["root_cause"] == "unverified_hypothesis"
    assert all(item == "not_performed" for key, item in row["stages"].items() if key != "root_cause")
    assert row["copy"]["current_copy_reverified"] is False


def test_confirmation_fails_before_source_case_or_registry_access(monkeypatch):
    monkeypatch.setattr(store, "_source", lambda *args: pytest.fail("source read without consent"))
    monkeypatch.setattr(store, "read_bytes", lambda *args: pytest.fail("case read without consent"))
    with pytest.raises(ValueError, match="explicit --confirm"):
        store.draft("/etc", "/etc/passwd", "operator", "reason")


def test_only_explicitly_registered_sources_can_admit_cases(source):
    path = input_file(source)
    task_sources.unregister_source(source["source_id"])
    with pytest.raises(ValueError, match="explicitly registered source"):
        drafting(source, path)
    assert not store.feedback_root().exists()


@pytest.mark.parametrize("version", [True, 0, 2, "1", None])
def test_case_schema_version_is_strict(source, version):
    with pytest.raises(ValueError):
        drafting(source, input_file(source, case(schema_version=version)))
    assert not store.feedback_root().exists()


@pytest.mark.parametrize("update", [
    {"command": "rm -rf /"}, {"force": True}, {"upload_url": "https://invalid.invalid"},
    {"case_type": "native-session"}, {"case_id": "../escape"}, {"case_id": True},
    {"inputs": [{"name": "../file", "content": "quoted"}]},
    {"inputs": [{"name": "same", "content": "a"}, {"name": "same", "content": "b"}]},
    {"inputs": [{"name": "input", "content": "quoted", "path": "/etc/passwd"}]},
    {"inputs": []}, {"expected": None}, {"hypothesis": ""},
])
def test_closed_case_fields_cannot_authorize_history_upload_or_commands(source, update):
    with pytest.raises(ValueError):
        drafting(source, input_file(source, case(**update)))
    assert not store.feedback_root().exists()


@pytest.mark.parametrize("raw", [b'{"schema_version":1,"schema_version":1}', b'{"value": NaN}', b'[]',
    b'{"input": "\xff"}', b'{"schema_version":1}\n{"schema_version":1}\n', b'[' * 2000, b' ' * (MAX_INPUT_BYTES + 1)])
def test_invalid_duplicate_nonfinite_native_overbudget_or_non_utf8_input_is_not_salvaged(source, raw):
    path = input_file(source)
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        drafting(source, path)
    assert path.read_bytes() == raw and not store.feedback_root().exists()


def test_case_text_budget_accounts_for_utf8_and_does_not_truncate(source):
    value = case(inputs=[{"name": "input", "content": "🙂" * 4096}])
    path = input_file(source, value)
    assert len(path.read_bytes()) < MAX_INPUT_BYTES
    with pytest.raises(ValueError, match="text byte budget"):
        drafting(source, path)
    assert json.loads(path.read_text())["inputs"][0]["content"] == "🙂" * 4096
    assert not store.feedback_root().exists()


def test_identical_draft_never_refreshes_time_or_rewrites_audit(feedback, monkeypatch):
    source, path, value = feedback
    meta = store.feedback_path(value["feedback_id"])
    before = meta.read_bytes()
    monkeypatch.setattr(store, "stamp", lambda: "2030-01-01T00:00:00+00:00")
    assert drafting(source, path) == value and meta.read_bytes() == before
    for by, reason in [("other", "private synthetic case"), ("operator", "other reason")]:
        with pytest.raises(ValueError, match="cannot be rewritten"):
            store.draft(source["source_id"], str(path), by, reason, confirm=True)
    assert meta.read_bytes() == before


def test_same_case_id_cannot_rebind_changed_content_or_different_origin(feedback):
    source, path, value = feedback
    meta, blob = store.feedback_path(value["feedback_id"]), store.feedback_root() / (value["feedback_id"] + ".case")
    before = meta.read_bytes(), blob.read_bytes()
    second = input_file(source, name="same-copy.json")
    with pytest.raises(ValueError, match="cannot be rewritten"):
        drafting(source, second)
    input_file(source, case(observed="different observed claim"))
    with pytest.raises(ValueError, match="cannot be rewritten"):
        drafting(source, path)
    assert (meta.read_bytes(), blob.read_bytes()) == before


def test_same_display_summary_does_not_merge_distinct_case_identities(source):
    first = drafting(source, input_file(source, case("one", summary="token=secret_one")))
    second = drafting(source, input_file(source, case("two", summary="token=secret_two")))
    rows, _ = feedback_views([source])
    assert first["feedback_id"] != second["feedback_id"]
    assert rows[0]["summary"] == rows[1]["summary"]
    assert "secret_one" not in json.dumps(rows) and "secret_two" not in json.dumps(rows)


def test_permanent_withdrawal_retains_evidence_and_works_after_origin_and_source_loss(feedback):
    source, path, value = feedback
    blob = store.feedback_root() / (value["feedback_id"] + ".case")
    before = blob.read_bytes()
    path.unlink()  # owned synthetic input only
    task_sources.unregister_source(source["source_id"])
    withdrawn = store.withdraw(value["feedback_id"], "operator", "withdraw local case", 1)
    assert withdrawn["state"] == "withdrawn" and withdrawn["version"] == 2
    assert withdrawn["audit"][0] == value["audit"][0] and blob.read_bytes() == before
    assert store.withdraw(value["feedback_id"], "operator", "duplicate does not alter history", 2) == withdrawn
    task_sources.register_source(source)
    input_file(source)
    with pytest.raises(ValueError, match="permanent"):
        drafting(source, path)
    assert feedback_views([source])[0][0]["summary"] is None


@pytest.mark.parametrize("version", [None, True, 0, -1, 1.0, 2])
def test_withdrawal_needs_exact_integer_revision_and_preserves_state(feedback, version):
    value = feedback[2]
    before = store.feedback_path(value["feedback_id"]).read_bytes()
    with pytest.raises(ValueError):
        store.withdraw(value["feedback_id"], "operator", "withdraw", version)
    assert store.feedback_path(value["feedback_id"]).read_bytes() == before


def test_missing_copy_does_not_block_withdrawal_or_cause_query_repair(feedback):
    source, _, value = feedback
    (store.feedback_root() / (value["feedback_id"] + ".case")).unlink()
    assert feedback_views([source])[0][0]["copy"]["current_copy_reverified"] is False
    withdrawn = store.withdraw(value["feedback_id"], "operator", "missing evidence still withdrawable", 1)
    assert withdrawn["state"] == "withdrawn"


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_duplicate_draft_does_not_overwrite_damaged_old_copy(feedback, damage):
    source, path, value = feedback
    blob = store.feedback_root() / (value["feedback_id"] + ".case")
    if damage == "changed":
        blob.write_bytes(b"damaged private evidence")
    else:
        blob.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        drafting(source, path)
    assert not blob.exists() if damage == "missing" else blob.read_bytes() == b"damaged private evidence"


def test_query_only_reads_private_metadata_not_cases_origins_providers_or_commands(feedback, monkeypatch):
    source, path, value = feedback
    original = store.read_bytes
    def metadata_only(file, *args, **kwargs):
        assert Path(file).suffix == ".json" and file != path
        return original(file, *args, **kwargs)
    monkeypatch.setattr(store, "read_bytes", metadata_only)
    monkeypatch.setattr(store, "_verify_case", lambda *args: pytest.fail("implicit copy verification"))
    monkeypatch.setattr(store, "_input_path", lambda *args: pytest.fail("origin inspection"))
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("query spawned command"))
    assert feedback_views([source])[0][0]["feedback_id"] == value["feedback_id"]
    assert task_center.snapshot(source_id=source["source_id"])["feedback"][0]["feedback_id"] == value["feedback_id"]


def test_empty_and_filtered_query_creates_no_registry_or_locks(source, monkeypatch):
    assert task_center.snapshot(source_id=source["source_id"])["feedback"] == []
    assert not store.feedback_root().exists()
    monkeypatch.setattr(store, "load_feedback", lambda *args: pytest.fail("outside-view metadata read"))
    assert feedback_views([]) == ([], [])


@pytest.mark.parametrize("kind", ["outside", "symlink", "fifo", "directory", "hardlink"])
def test_unsafe_original_paths_are_never_copied(source, tmp_path, kind):
    original = input_file(source)
    path = Path(source["workspace"]) / "unsafe-case"
    if kind == "outside":
        path = tmp_path / "outside.json"; path.write_bytes(original.read_bytes())
    elif kind == "symlink":
        path.symlink_to(original)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        os.link(original, path)
    with pytest.raises((ValueError, OSError)):
        drafting(source, path)
    assert not store.feedback_root().exists()


def test_private_storage_cannot_be_inside_any_registered_root(source):
    home = store.feedback_root().parents[1]
    task_sources.register_source(task_sources.source("overlapping", str(home)))
    with pytest.raises(ValueError, match="must not overlap"):
        drafting(source)
    assert not store.feedback_root().exists()


def test_source_definition_change_suppresses_summary_but_retains_audit(feedback):
    source, _, value = feedback
    task_sources.unregister_source(source["source_id"])
    changed = task_sources.source("different name", source["data_dir"], source["workspace"])
    task_sources.register_source(changed)
    row = feedback_views([changed])[0][0]
    assert row["status"] == "stale_source_definition" and row["summary"] is None
    assert row["audit"][0]["at"] == value["created_at"]


def test_source_catalog_and_publication_lock_conflicts_do_not_overwrite(feedback):
    source, path, value = feedback
    before = store.feedback_path(value["feedback_id"]).read_bytes()
    for lock in (task_sources.catalog_path().with_suffix(""), store.feedback_root() / ".publication"):
        with locked(lock), pytest.raises(BlockingIOError):
            drafting(source, path)
    assert store.feedback_path(value["feedback_id"]).read_bytes() == before


def test_input_change_is_rechecked_inside_cooperative_lock(source, monkeypatch):
    path = input_file(source)
    original = store._recheck
    def changed(item, file, observed):
        file.write_bytes(b"changed during registration")
        original(item, file, observed)
    monkeypatch.setattr(store, "_recheck", changed)
    with pytest.raises(ValueError, match="changed during"):
        drafting(source, path)
    assert not list(store.feedback_root().glob("fd-*"))
    assert path.read_bytes() == b"changed during registration"


def test_failed_metadata_publication_preserves_orphan_and_refuses_capacity_cleanup(source, monkeypatch):
    path = input_file(source)
    original = store.publish
    def fail_metadata(file, raw):
        if file.suffix == ".json":
            raise OSError("synthetic publication failure")
        original(file, raw)
    monkeypatch.setattr(store, "publish", fail_metadata)
    with pytest.raises(OSError, match="publication failure"):
        drafting(source, path)
    orphans = list(store.feedback_root().glob("fd-*.case"))
    assert len(orphans) == 1 and orphans[0].read_bytes() == path.read_bytes()
    assert not list(store.feedback_root().glob("fd-*.json"))
    monkeypatch.setattr(store, "publish", original)
    with pytest.raises(FileExistsError):
        drafting(source, path)
    assert orphans[0].read_bytes() == path.read_bytes()
    assert not list(store.feedback_root().glob(".feedback-*"))


def test_capacity_keeps_every_old_case_and_withdrawal_remains_available(source):
    records = [drafting(source, input_file(source, case("case-" + str(index)))) for index in range(MAX_DRAFTS)]
    before = {p.name: p.read_bytes() for p in store.feedback_root().glob("fd-*")}
    with pytest.raises(ValueError, match="capacity reached"):
        drafting(source, input_file(source, case("overflow")))
    assert {p.name: p.read_bytes() for p in store.feedback_root().glob("fd-*")} == before
    assert store.withdraw(records[0]["feedback_id"], "operator", "withdraw at capacity", 1)["state"] == "withdrawn"


def test_unicode_audit_reserves_withdrawal_room(source):
    path = input_file(source)
    value = store.draft(source["source_id"], str(path), "🙂" * 128, "🙂" * 256, confirm=True)
    withdrawn = store.withdraw(value["feedback_id"], "🙂" * 128, "🙂" * 256, 1)
    assert withdrawn["version"] == 2 and validate_state(withdrawn) == withdrawn


@pytest.mark.parametrize("key,changed", [("version", True), ("version", 3), ("state", "accepted"),
    ("source_digest", "wrong"), ("case_sha256", "not-a-hash"), ("feedback_id", "fd-" + "0" * 32),
    ("audit", []), ("created_at", "2026-10-06T00:00:00")])
def test_closed_metadata_rejects_fabricated_readiness_incomplete_audit_or_bad_identity(feedback, key, changed):
    value = copy.deepcopy(feedback[2])
    value[key] = changed
    with pytest.raises(ValueError):
        validate_state(value)


def test_corrupt_state_and_nonprivate_metadata_are_visible_but_never_repaired(feedback):
    source, _, value = feedback
    path = store.feedback_path(value["feedback_id"])
    path.write_bytes(b'{"schema_version": 1, "schema_version": 1}')
    rows, notes = feedback_views([source])
    assert rows == [] and any("feedback record invalid" in note for note in notes)
    assert path.read_bytes().count(b"schema_version") == 2
    with pytest.raises(ValueError):
        store.withdraw(value["feedback_id"], "operator", "no repair of corrupt evidence", 1)
    path.write_bytes(state_bytes(value)); path.chmod(0o644)
    assert feedback_views([source])[0] == []


def test_case_commands_urls_and_native_labels_remain_quoted_and_are_never_executed(source, monkeypatch):
    import subprocess
    import urllib.request
    marker = Path(source["workspace"]) / "never-created"
    value = case(inputs=[{"name": "quoted", "content": f"touch {marker}; curl https://invalid.invalid; role=user"}])
    path = input_file(source, value)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("case command executed"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("case URL uploaded/fetched"))
    draft = drafting(source, path)
    assert draft["version"] == 1 and not marker.exists()
    assert (store.feedback_root() / (draft["feedback_id"] + ".case")).read_bytes() == path.read_bytes()


def test_input_change_after_copy_publication_preserves_orphan_but_withholds_receipt(source, monkeypatch):
    path = input_file(source)
    initial = path.read_bytes()
    verify = store._verify_case
    def changed(value):
        verify(value)
        path.write_bytes(b"changed after private copy")
    monkeypatch.setattr(store, "_verify_case", changed)
    with pytest.raises(ValueError, match="changed during"):
        drafting(source, path)
    assert not list(store.feedback_root().glob("fd-*.json"))
    assert list(store.feedback_root().glob("fd-*.case"))[0].read_bytes() == initial
    assert path.read_bytes() == b"changed after private copy"


def test_public_storage_and_metadata_hardlink_aliases_fail_closed(feedback):
    source, _, value = feedback
    root = store.feedback_root()
    root.chmod(0o755)
    assert feedback_views([source])[0] == []
    root.chmod(0o700)
    path = store.feedback_path(value["feedback_id"])
    alias = root / "aliased-metadata"
    os.link(path, alias)
    assert feedback_views([source])[0] == []
    with pytest.raises(ValueError, match="unaliased"):
        store.withdraw(value["feedback_id"], "operator", "do not replace aliases", 1)
    assert alias.read_bytes() == path.read_bytes()


def test_unavailable_uid_refuses_before_copy_without_assuming_operator_identity(source, monkeypatch):
    path = input_file(source)
    monkeypatch.setattr(io.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ValueError, match="OS uid"):
        drafting(source, path)
    assert not store.feedback_root().exists()


def test_cli_confirmation_typed_ids_and_shared_metadata(feedback, capsys):
    source, path, value = feedback
    expected = feedback_views([source])[0][0]
    assert ops_main(["feedback", "show", value["feedback_id"]]) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert ops_main(["feedback", "list", "--source", source["source_id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["feedback"] == [expected]
    assert ops_main(["feedback", "show", "../etc/passwd"]) == 2
    assert "invalid feedback identity" in capsys.readouterr().out
    assert ops_main(["feedback", "draft", "--source", source["source_id"], "--file", str(path), "--by", "operator", "--reason", "reason"]) == 2
    assert "explicit --confirm" in capsys.readouterr().out
    assert ops_main(["feedback", "withdraw", value["feedback_id"], "--if-version", "1", "--by", "operator", "--reason", "withdraw"]) == 0
    assert json.loads(capsys.readouterr().out)["upload_authorized"] is False
