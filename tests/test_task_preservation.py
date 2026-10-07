"""Private single-file snapshots are byte evidence, never complete archive or cleanup authority."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from test_task_retention import source, retained, _view, _keep
from pbatch import task_preservation as preservation, task_preservation_io as io
from pbatch import task_retention_store as store, task_sources, task_center
from pbatch.task_preservation_schema import validate_receipt
from pbatch.task_state_io import locked
from pbatch.ops import ops_main


def _copy(retained):
    return preservation.preserve(retained[2]["resource_id"], "operator", "save failed result", 1, confirm=True)


def _blob(value):
    return preservation.preservation_root() / (value["preservation_id"] + ".blob")


def test_copy_full_bytes_private_exclusive_and_no_policy_or_original_writes(retained):
    _, file, resource = retained
    original = file.read_bytes(), store.resource_path(resource["resource_id"]).read_bytes(), store.observed_stat(file)
    value = _copy(retained)
    assert _blob(value).read_bytes() == original[0]
    assert value["payload"] == {"bytes": len(original[0]), "sha256": hashlib.sha256(original[0]).hexdigest()}
    assert _blob(value).stat().st_mode & 0o777 == 0o600
    assert preservation.receipt_path(value["preservation_id"]).stat().st_mode & 0o777 == 0o600
    assert preservation.preservation_root().stat().st_mode & 0o777 == 0o700
    assert preservation.preservation_root() not in file.parents
    assert (file.read_bytes(), store.resource_path(resource["resource_id"]).read_bytes(), store.observed_stat(file)) == original
    assert not list(preservation.preservation_root().glob(".preservation-*"))
    validate_receipt(value)
    row = _view(retained)
    _keep(row)
    assert row["preservation"]["observed_receipts"] == 1
    assert row["preservation"]["receipts"][0]["current_payload_reverified"] is False
    assert row["preservation"]["complete_archive_verified"] is False


def test_identical_copy_never_rewrites_receipt_or_renews_retention_time(retained):
    value = _copy(retained)
    paths = preservation.receipt_path(value["preservation_id"]), _blob(value)
    before = [store.observed_stat(path) for path in paths]
    assert _copy(retained) == value
    assert [store.observed_stat(path) for path in paths] == before
    with pytest.raises(ValueError, match="audit cannot"):
        preservation.preserve(retained[2]["resource_id"], "someone else", "new reason", 1, confirm=True)
    assert [store.observed_stat(path) for path in paths] == before


def test_copy_empty_binary_and_changed_same_object_bytes_need_no_text_or_old_stat_rewrite(retained):
    _, file, resource = retained
    registry = store.resource_path(resource["resource_id"]).read_bytes()
    for data in (b"", b"\0\xffprivate\0", b"changed source version"):
        file.write_bytes(data)
        value = _copy(retained)
        assert _blob(value).read_bytes() == data and value["source_stat"] == store.observed_stat(file)
    assert store.resource_path(resource["resource_id"]).read_bytes() == registry
    assert _view(retained)["preservation"]["observed_receipts"] == 3
    _keep(_view(retained))


@pytest.mark.parametrize("action", ["preserve", "verify"])
def test_explicit_confirmation_before_any_request_resource_or_payload_reads(monkeypatch, action):
    monkeypatch.setattr(preservation, "load_resource", lambda *args: pytest.fail("source access before consent"))
    monkeypatch.setattr(preservation, "load_receipt", lambda *args: pytest.fail("payload access before consent"))
    with pytest.raises(ValueError, match="--confirm"):
        if action == "preserve":
            preservation.preserve("rr-" + "0" * 32, "operator", "reason", 1)
        else:
            preservation.verify_copy("pp-" + "0" * 32)
    assert not preservation.preservation_root().exists()


@pytest.mark.parametrize("damage", ["revision", "source", "replacement", "symlink", "kind", "byte_budget", "archive_scope", "uid"])
def test_unsafe_incomplete_or_changed_scope_never_opens_source_body(retained, monkeypatch, damage):
    source, file, resource = retained
    if damage == "revision":
        store.protect(resource["resource_id"], "operator", "pin", 1)
    elif damage == "source":
        task_sources.unregister_source(source["source_id"])
    elif damage == "replacement":
        other = file.with_name("other")
        other.write_text("different physical identity")
        other.replace(file)
    elif damage == "symlink":
        file.unlink()
        file.symlink_to(Path(source["workspace"]) / "private-source")
    elif damage == "kind":
        directory = Path(source["data_dir"]) / "worktree"
        directory.mkdir()
        resource = store.register(source["source_id"], str(directory), "worktree", "operator", "preserve", confirm=True)
    elif damage == "byte_budget":
        monkeypatch.setattr(preservation, "MAX_PAYLOAD_BYTES", 1)
    elif damage == "archive_scope":
        monkeypatch.setattr(preservation, "preservation_root", lambda: Path(source["data_dir"]) / "copies")
    else:
        monkeypatch.setattr(store.os, "geteuid", lambda: -1)
    monkeypatch.setattr(preservation, "copy_stream", lambda *args: pytest.fail("unsafe source body read"))
    with pytest.raises((ValueError, OSError)):
        preservation.preserve(resource["resource_id"], "operator", "preserve", 1, confirm=True)


def test_source_mutation_during_copy_leaves_no_receipt_and_preserves_changed_source(retained, monkeypatch):
    original = preservation.copy_stream
    def racing(path, output, expected):
        result = original(path, output, expected)
        if path == retained[1]:
            path.write_text("concurrent changed outcome")
        return result
    monkeypatch.setattr(preservation, "copy_stream", racing)
    with pytest.raises(ValueError, match="changed"):
        _copy(retained)
    assert retained[1].read_text() == "concurrent changed outcome"
    assert not list(preservation.preservation_root().glob("pp-*"))
    assert not list(preservation.preservation_root().glob(".preservation-*"))


def test_resource_or_source_revision_change_during_copy_withholds_publication(retained, monkeypatch):
    original = preservation.copy_stream
    def changing(path, output, expected):
        result = original(path, output, expected)
        if path == retained[1]:
            value = store.load_resource(retained[2]["resource_id"])
            value["version"] = 2
            monkeypatch.setattr(preservation, "load_resource", lambda *args: value)
        return result
    monkeypatch.setattr(preservation, "copy_stream", changing)
    with pytest.raises(ValueError, match="revision conflicts"):
        _copy(retained)
    assert not list(preservation.preservation_root().glob("pp-*"))


@pytest.mark.parametrize("lock", ["catalog", "publication"])
def test_cooperative_lock_conflicts_refuse_without_copy_or_breaking_lock(retained, monkeypatch, lock):
    path = store.resource_root() / ".catalog" if lock == "catalog" else preservation.preservation_root() / ".publication"
    monkeypatch.setattr(preservation, "copy_stream", lambda *args: pytest.fail("copy under busy lock"))
    with locked(path), pytest.raises((OSError, ValueError)):
        _copy(retained)
    assert retained[1].exists()


@pytest.mark.parametrize("damage", ["contents", "missing", "symlink", "receipt", "mode"])
def test_verification_of_unavailable_changed_or_public_copy_refuses_without_repair(retained, damage):
    value = _copy(retained)
    blob = _blob(value)
    if damage == "contents":
        blob.write_bytes(b"changed preservation data")
    elif damage == "missing":
        blob.unlink()
    elif damage == "symlink":
        blob.unlink()
        blob.symlink_to(retained[1])
    elif damage == "receipt":
        preservation.receipt_path(value["preservation_id"]).write_text('{"schema_version":1,"unsafe":true}')
    else:
        blob.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        preservation.verify_copy(value["preservation_id"], confirm=True)
    _keep(_view(retained))
    assert retained[1].read_bytes() == b"private recovery evidence\n"


def test_orphaned_payload_after_publication_error_is_retained_not_silently_retried_or_reclaimed(retained, monkeypatch):
    original = io.os.link
    def interrupted(source, destination):
        if Path(destination).suffix == ".json":
            raise OSError("synthetic receipt publication failure")
        return original(source, destination)
    with monkeypatch.context() as context:
        context.setattr(io.os, "link", interrupted)
        with pytest.raises(OSError, match="publication failure"):
            _copy(retained)
    blobs = list(preservation.preservation_root().glob("pp-*.blob"))
    assert len(blobs) == 1 and blobs[0].read_bytes() == retained[1].read_bytes()
    assert not list(preservation.preservation_root().glob("*.json"))
    with pytest.raises(ValueError, match="collision"):
        _copy(retained)
    assert blobs[0].exists()


def test_full_copy_hash_survives_source_or_catalog_loss_without_claiming_recoverability(retained):
    value = _copy(retained)
    retained[1].unlink()
    task_sources.unregister_source(retained[0]["source_id"])
    result = preservation.verify_copy(value["preservation_id"], confirm=True)
    assert result["verification"] == "recorded_copy_bytes_matching"
    assert result["current_source_checked"] is False and result["recoverable_archive_verified"] is False
    assert result["cleanup_authority"] == "not_granted" and result["grants_execution"] is False


@pytest.mark.parametrize("mode", ["root", "snapshot_type", "bad_digest", "duplicate", "oversized", "unsafe_id"])
def test_malformed_receipts_fail_closed_without_payload_rewrites(retained, mode):
    value = _copy(retained)
    file = preservation.receipt_path(value["preservation_id"])
    if mode == "root":
        preservation.preservation_root().chmod(0o755)
    elif mode == "snapshot_type":
        value["source_stat"]["mode"] = 0o40700
        file.write_text(json.dumps(value))
    elif mode == "bad_digest":
        value["payload"]["sha256"] = "f" * 64
        file.write_text(json.dumps(value))
    elif mode == "duplicate":
        file.write_text(file.read_text().replace('"schema_version":1', '"schema_version":1,"schema_version":1'))
    elif mode == "oversized":
        file.write_text(" " * 9000)
    with pytest.raises(ValueError):
        preservation.verify_copy("../../secret" if mode == "unsafe_id" else value["preservation_id"], confirm=True)
    assert _blob(value).read_bytes() == retained[1].read_bytes()


def test_default_queries_do_not_open_copy_or_source_bodies_and_do_not_verify_implicitly(retained, monkeypatch):
    value = _copy(retained)
    monkeypatch.setattr(preservation, "copy_stream", lambda *args: pytest.fail("implicit data read"))
    monkeypatch.setattr(preservation, "verify_copy", lambda *args, **kwargs: pytest.fail("implicit byte verification"))
    row = _view(retained)
    assert row["preservation"]["receipts"][0]["preservation_id"] == value["preservation_id"]
    _keep(row)


def test_capacity_never_removes_old_copies_and_duplicate_can_reuse_at_capacity(retained, monkeypatch):
    value = _copy(retained)
    monkeypatch.setattr(preservation, "MAX_RECEIPTS", 1)
    assert _copy(retained) == value
    retained[1].write_text("next content to preserve")
    with pytest.raises(ValueError, match="capacity"):
        _copy(retained)
    assert _blob(value).read_bytes() == b"private recovery evidence\n"


def test_cli_copy_and_verify_are_explicit_hash_success_not_disposal_admission(retained, capsys):
    key = retained[2]["resource_id"]
    base = ["retention", "preserve", key, "--if-version", "1", "--by", "operator", "--reason", "save failed result"]
    assert ops_main(base) == 2
    capsys.readouterr()
    assert ops_main(base + ["--confirm"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["recoverable_archive_verified"] is False and result["cleanup_authority"] == "not_granted"
    assert ops_main(["retention", "verify-copy", result["preservation_id"], "--confirm"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["current_source_checked"] is False
    _keep(_view(retained))


def test_temporary_receipt_name_collision_never_deletes_an_existing_file(retained, monkeypatch):
    original = io.publish
    collisions = []
    def conflicting(temporary, receipt, root):
        staged = temporary.with_suffix(".receipt")
        staged.write_text("preexisting another command evidence")
        collisions.append(staged)
        return original(temporary, receipt, root)
    monkeypatch.setattr(preservation, "publish", conflicting)
    with pytest.raises(OSError):
        _copy(retained)
    assert collisions[0].read_text() == "preexisting another command evidence"
    assert len(list(preservation.preservation_root().glob("pp-*.blob"))) == 1


def test_own_temp_cleanup_refuses_replaced_inode_without_removing_other_file(tmp_path):
    temporary = tmp_path / "temporary"
    temporary.write_text("owned initial")
    created = temporary.stat()
    other = tmp_path / "replacement"
    other.write_text("other command file")
    other.replace(temporary)
    io.remove_owned_temporary(temporary, created)
    assert temporary.read_text() == "other command file"


def test_corrupt_original_payload_is_preserved_and_repeated_save_never_overwrites_it(retained):
    value = _copy(retained)
    _blob(value).write_text("changed old payload must be kept")
    before = preservation.receipt_path(value["preservation_id"]).read_bytes()
    with pytest.raises(ValueError, match="original recorded bytes"):
        _copy(retained)
    assert _blob(value).read_text() == "changed old payload must be kept"
    assert preservation.receipt_path(value["preservation_id"]).read_bytes() == before


def test_full_readback_failure_retains_published_evidence_not_claims_copy_success(retained, monkeypatch):
    original = preservation.copy_stream
    def corrupt(path, output, expected):
        if path.suffix == ".blob":
            path.write_text("synthetic damage before readback")
        return original(path, output, expected)
    monkeypatch.setattr(preservation, "copy_stream", corrupt)
    with pytest.raises(ValueError, match="changed"):
        _copy(retained)
    assert len(list(preservation.preservation_root().glob("pp-*.blob"))) == 1
    assert len(list(preservation.preservation_root().glob("pp-*.json"))) == 1
    _keep(_view(retained))


def test_receipt_display_counts_omissions_never_reads_bodies_on_refresh(retained):
    for number in range(10):
        retained[1].write_text("source snapshot " + str(number))
        _copy(retained)
    row = _view(retained)["preservation"]
    assert row["observed_receipts"] == 10 and len(row["receipts"]) == 8 and row["omitted_receipts"] == 2
    assert row["complete_archive_verified"] is False


@pytest.mark.parametrize("field,value", [("delete", True), ("schema_version", True), ("resource_version", True),
                                         ("source_digest", "invalid"), ("source_stat", {}), ("payload", {})])
def test_receipts_are_closed_strict_immutable_byte_claims(retained, field, value):
    receipt = _copy(retained)
    changed = copy.deepcopy(receipt)
    changed[field] = value
    with pytest.raises(ValueError):
        validate_receipt(changed)
