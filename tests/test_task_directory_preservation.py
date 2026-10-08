"""Explicit directory bytes are private evidence, not worktree recovery or cleanup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tarfile

import pytest

from test_task_retention import source, _view, _keep
from pbatch import task_directory_preservation as directory, task_directory_archive as archive
from pbatch import task_directory_inventory as inventory, task_preservation as preservation
from pbatch import task_retention_store as store, task_sources
from pbatch.task_preservation_schema import validate_receipt
from pbatch.task_state_io import locked
from pbatch.ops import ops_main


@pytest.fixture
def saved_tree(source):
    root = Path(source["data_dir"]) / "chosen-directory"
    (root / "nested").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "nested" / "private-body.txt").write_bytes(b"PRIVATE-DIRECTORY-BODY; rm -rf quoted-never-run\0\xff")
    (root / "run.sh").write_bytes(b"https://invalid.example/quoted; never execute\n")
    (root / "run.sh").chmod(0o700)
    (root / "zero").touch()
    value = store.register(source["source_id"], str(root), "directory", "operator", "keep bytes", confirm=True)
    return source, root, value


def _copy(value):
    return directory.preserve_directory(value[2]["resource_id"], "operator", "keep directory bytes", 1, confirm=True)


def _blob(value):
    return preservation.preservation_root() / (value["preservation_id"] + ".blob")


def _samples(root):
    return {path.relative_to(root).as_posix(): store.observed_stat(path) for path in [root, *root.rglob("*")]}


def test_full_empty_binary_nested_and_executable_members_saved_privately_without_original_writes(saved_tree, monkeypatch):
    import subprocess
    import urllib.request
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("command/provider execution"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network upload"))
    before = _samples(saved_tree[1]), store.resource_path(saved_tree[2]["resource_id"]).read_bytes()
    value = _copy(saved_tree)
    assert value["schema_version"] == 2
    validate_receipt(value)
    summary = value["directory_copy"]
    assert (summary["entries"], summary["files"], summary["directories"]) == (6, 3, 3)
    assert _blob(value).stat().st_mode & 0o777 == 0o600
    assert preservation.receipt_path(value["preservation_id"]).stat().st_mode & 0o777 == 0o600
    assert preservation.preservation_root().stat().st_mode & 0o777 == 0o700
    assert value["payload"]["sha256"] == hashlib.sha256(_blob(value).read_bytes()).hexdigest()
    with tarfile.open(_blob(value), "r:", encoding="utf-8", errors="strict") as copied:
        assert copied.getnames() == [".", "empty", "nested", "nested/private-body.txt", "run.sh", "zero"]
        for member in copied:
            assert member.type in {tarfile.REGTYPE, tarfile.DIRTYPE}
            assert member.uid == member.gid == member.mtime == 0
            if member.isfile():
                assert copied.extractfile(member).read() == (saved_tree[1] / member.name).read_bytes()
        assert copied.getmember("run.sh").mode == 0o700
    assert (_samples(saved_tree[1]), store.resource_path(saved_tree[2]["resource_id"]).read_bytes()) == before
    assert not list(preservation.preservation_root().glob(".preservation-*"))
    _keep(_view(saved_tree))


def test_same_bytes_are_noop_without_audit_or_review_renewal_and_changed_bytes_get_new_receipt(saved_tree):
    value = _copy(saved_tree)
    before = preservation.receipt_path(value["preservation_id"]).read_bytes(), _blob(value).stat()
    assert _copy(saved_tree) == value
    assert (preservation.receipt_path(value["preservation_id"]).read_bytes(), _blob(value).stat()) == before
    with pytest.raises(ValueError, match="audit cannot"):
        directory.preserve_directory(saved_tree[2]["resource_id"], "other", "changed reason", 1, confirm=True)
    old_bytes = _blob(value).read_bytes()
    (saved_tree[1] / "run.sh").write_bytes(b"different body same object")
    changed = _copy(saved_tree)
    assert changed["preservation_id"] != value["preservation_id"]
    assert _blob(value).read_bytes() == old_bytes
    assert store.load_resource(saved_tree[2]["resource_id"]) == saved_tree[2]


def test_selected_empty_directory_and_utf8_member_names_preserve_every_selected_entry(saved_tree):
    for path in sorted(saved_tree[1].rglob("*"), key=lambda item: len(item.parts), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()  # only this test's synthetic input
    empty = _copy(saved_tree)
    assert (empty["directory_copy"]["entries"], empty["directory_copy"]["files"], empty["directory_copy"]["content_bytes"]) == (1, 0, 0)
    name = "é" * 49
    (saved_tree[1] / name).write_bytes(b"utf8-name-full-body\0")
    value = _copy(saved_tree)
    assert value["directory_copy"]["entries"] == 2
    with tarfile.open(_blob(value), "r:", encoding="utf-8", errors="strict") as copied:
        assert copied.getnames() == [".", name]
        assert copied.extractfile(name).read() == b"utf8-name-full-body\0"


def test_exact_entry_capacity_includes_root_and_never_omits_one_more_child(saved_tree):
    for index in range(250):
        (saved_tree[1] / ("entry-%03d" % index)).touch()
    value = _copy(saved_tree)
    assert value["directory_copy"]["entries"] == 256
    (saved_tree[1] / "one-too-many").touch()
    with pytest.raises(ValueError, match="entry budget"):
        _copy(saved_tree)
    assert preservation.verify_copy(value["preservation_id"], confirm=True)["directory_copy"]["entries"] == 256
    assert (saved_tree[1] / "one-too-many").exists()


def test_unconfirmed_directory_copy_reads_no_source_names_metadata_or_payload(monkeypatch):
    monkeypatch.setattr(directory, "load_resource", lambda *a: pytest.fail("resource read without confirmation"))
    monkeypatch.setattr(directory, "copy_directory", lambda *a: pytest.fail("names/body read without confirmation"))
    with pytest.raises(ValueError, match="--confirm"):
        directory.preserve_directory("rr-" + "0" * 32, "operator", "copy", 1)
    assert not preservation.preservation_root().exists()


@pytest.mark.parametrize("damage", ["revision", "replacement", "definition", "storage_scope", "worktree", "file"])
def test_registration_scope_identity_kind_and_revision_are_not_widened(saved_tree, monkeypatch, damage):
    key = saved_tree[2]["resource_id"]
    if damage == "revision":
        store.protect(key, "operator", "pin", 1)
    elif damage == "replacement":
        saved_tree[1].rename(saved_tree[1].with_name("original-preserved"))
        saved_tree[1].mkdir()
    elif damage == "definition":
        task_sources.unregister_source(saved_tree[0]["source_id"])
    elif damage == "storage_scope":
        monkeypatch.setattr(preservation, "preservation_root", lambda: saved_tree[1] / "copies")
    else:
        extra = Path(saved_tree[0]["data_dir"]) / "other-resource"
        extra.mkdir() if damage == "worktree" else extra.touch()
        value = store.register(saved_tree[0]["source_id"], str(extra), damage, "operator", "keep", confirm=True)
        key = value["resource_id"]
    monkeypatch.setattr(directory, "copy_directory", lambda *a: pytest.fail("unsafe body read"))
    with pytest.raises((ValueError, OSError)):
        directory.preserve_directory(key, "operator", "keep", 1, confirm=True)


@pytest.mark.parametrize("alias", ["file_symlink", "directory_symlink", "hardlink", "fifo", "git_pointer", "git_directory", "privileged", "unsafe_name"])
def test_alias_special_git_and_unsafe_names_refuse_before_any_member_body_read(saved_tree, monkeypatch, alias):
    root = saved_tree[1]
    if alias == "file_symlink":
        (root / "alias").symlink_to(root / "run.sh")
    elif alias == "directory_symlink":
        (root / "alias").symlink_to(root / "nested", target_is_directory=True)
    elif alias == "hardlink":
        os.link(root / "run.sh", root / "alias")
    elif alias == "fifo":
        os.mkfifo(root / "pipe")
    elif alias == "git_pointer":
        (root / ".git").write_text("gitdir: /private/outside-do-not-read\n")
    elif alias == "git_directory":
        (root / ".git").mkdir()
    elif alias == "privileged":
        (root / "run.sh").chmod(0o4700)
    else:
        (root / "unsafe\u202ename").touch()
    monkeypatch.setattr(archive, "copy_stream", lambda *a: pytest.fail("body read before inventory admission"))
    with pytest.raises((ValueError, OSError)):
        _copy(saved_tree)
    assert not list(preservation.preservation_root().glob("pp-*"))


@pytest.mark.parametrize("bound", ["entries", "content", "container", "depth", "path"])
def test_each_independent_budget_refuses_without_truncation_or_receipt(saved_tree, monkeypatch, bound):
    if bound == "entries":
        monkeypatch.setattr(inventory, "MAX_ENTRIES", 2)
    elif bound == "content":
        monkeypatch.setattr(inventory, "MAX_PAYLOAD_BYTES", 1)
    elif bound == "container":
        monkeypatch.setattr(archive, "MAX_PAYLOAD_BYTES", 1024)
    elif bound == "depth":
        deep = saved_tree[1]
        for _ in range(17):
            deep /= "d"
            deep.mkdir()
    else:
        (saved_tree[1] / ("x" * 100)).touch()
    with pytest.raises(ValueError, match="budget|profile"):
        _copy(saved_tree)
    assert not list(preservation.preservation_root().glob("pp-*"))
    assert (saved_tree[1] / "run.sh").read_bytes().startswith(b"https:")


@pytest.mark.parametrize("change", ["body", "add", "rename", "source", "revision"])
def test_changes_during_copy_withhold_receipt_without_repairing_or_discarding_source(saved_tree, monkeypatch, change):
    original = archive.copy_stream
    done = False
    def racing(path, output, expected):
        nonlocal done
        result = original(path, output, expected)
        if not done:
            done = True
            if change == "body":
                path.write_bytes(b"concurrent same-path modification")
            elif change == "add":
                (saved_tree[1] / "new-outcome").write_text("unsaved work must remain")
            elif change == "rename":
                path.rename(path.with_name("moved-outcome"))
            elif change == "source":
                task_sources.unregister_source(saved_tree[0]["source_id"])
            else:
                value = dict(saved_tree[2], version=2)
                monkeypatch.setattr(directory, "load_resource", lambda *a: value)
        return result
    monkeypatch.setattr(archive, "copy_stream", racing)
    with pytest.raises((ValueError, OSError)):
        _copy(saved_tree)
    assert done and saved_tree[1].exists()
    assert not list(preservation.preservation_root().glob("pp-*"))


@pytest.mark.parametrize("lock", ["catalog", "publication"])
def test_busy_locks_refuse_without_copy_or_lock_breaking(saved_tree, monkeypatch, lock):
    path = store.resource_root() / ".catalog" if lock == "catalog" else preservation.preservation_root() / ".publication"
    monkeypatch.setattr(directory, "copy_directory", lambda *a: pytest.fail("copy under conflicting lock"))
    with locked(path), pytest.raises((ValueError, OSError)):
        _copy(saved_tree)
    assert saved_tree[1].exists()


def test_explicit_verify_saved_directory_survives_original_source_loss_without_current_source_or_restore_reads(saved_tree, monkeypatch):
    value = _copy(saved_tree)
    saved_tree[1].rename(saved_tree[1].with_name("preserved-original-outside-view"))
    task_sources.unregister_source(saved_tree[0]["source_id"])
    monkeypatch.setattr(directory, "load_resource", lambda *a: pytest.fail("original resource read"))
    monkeypatch.setattr(archive, "inventory", lambda *a: pytest.fail("original directory reread"))
    monkeypatch.setattr(tarfile.TarFile, "extract", lambda *a, **k: pytest.fail("restore"))
    monkeypatch.setattr(tarfile.TarFile, "extractall", lambda *a, **k: pytest.fail("restore"))
    result = preservation.verify_copy(value["preservation_id"], confirm=True)
    assert result["directory_copy"] == value["directory_copy"]
    assert result["current_source_checked"] is result["recoverable_archive_verified"] is False
    assert result["cleanup_authority"] == "not_granted" and result["grants_execution"] is False


def test_saved_directory_corruption_is_not_overwritten_on_explicit_repeat(saved_tree):
    value = _copy(saved_tree)
    _blob(value).write_bytes(b"damaged copy must remain")
    with pytest.raises(ValueError, match="recorded bytes"):
        _copy(saved_tree)
    assert _blob(value).read_bytes() == b"damaged copy must remain"


def test_cli_preserve_directory_and_verify_are_hash_success_not_recovery_or_disposal(saved_tree, capsys):
    args = ["retention", "preserve-directory", saved_tree[2]["resource_id"], "--if-version", "1",
            "--by", "operator", "--reason", "keep directory bytes"]
    assert ops_main(args) == 2
    capsys.readouterr()
    assert ops_main(args + ["--confirm"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["directory_copy"]["entries"] == 6
    assert value["recoverable_archive_verified"] is False and value["cleanup_authority"] == "not_granted"
    assert ops_main(["retention", "verify-copy", value["preservation_id"], "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["directory_copy"] == value["directory_copy"]
    _keep(_view(saved_tree))
