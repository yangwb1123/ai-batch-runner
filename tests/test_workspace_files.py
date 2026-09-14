"""Filesystem safety tests for workspace packing and restore."""

import base64
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import pbatch.workspace_files as workspace_files
from pbatch.workspace_bundle import SCHEMA, WorkspaceBundleError
from pbatch.workspace_files import pack_directory, restore_bundle


def _bundle(files):
    return {"schema": SCHEMA, "files": [
        {"path": path,
         "content_b64": base64.b64encode(content).decode("ascii")}
        for path, content in files
    ]}


def test_pack_directory_sorts_regular_files_and_respects_allowlist(tmp_path):
    root = tmp_path / "source"
    (root / "nested").mkdir(parents=True)
    (root / "z.txt").write_bytes(b"last")
    (root / "nested" / "a.txt").write_bytes(b"first")

    packed = pack_directory(root)
    assert [item["path"] for item in packed["files"]] == [
        "nested/a.txt", "z.txt"]
    selected = pack_directory(root, ["z.txt"])
    assert [item["path"] for item in selected["files"]] == ["z.txt"]
    with pytest.raises(WorkspaceBundleError):
        pack_directory(root, {"not": "a list"})


def test_pack_directory_rejects_symlink_roots_files_and_directories(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link.txt").symlink_to(outside)
    with pytest.raises(WorkspaceBundleError):
        pack_directory(root)
    with pytest.raises(WorkspaceBundleError):
        pack_directory(root, ["link.txt"])

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(WorkspaceBundleError):
        pack_directory(linked_root)

    (root / "link.txt").unlink()
    (root / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkspaceBundleError):
        pack_directory(root)


def test_pack_root_does_not_resolve_a_raced_symlink(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "safe.txt").write_text("safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    moved = tmp_path / "moved-root"
    real_resolve = Path.resolve

    def replace_before_resolve(path, *args, **kwargs):
        if path == root:
            root.rename(moved)
            root.symlink_to(outside, target_is_directory=True)
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", replace_before_resolve)
    packed = pack_directory(root)
    assert packed["files"] == [{"path": "safe.txt", "content_b64": "c2FmZQ=="}]


def test_read_file_rejects_fifo_replaced_after_stat(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "item").write_bytes(b"safe")
    parent_fd = os.open(root, workspace_files._directory_flags())
    real_open = os.open

    def replace_with_fifo(path, flags, *args, dir_fd=None, **kwargs):
        if path == "item" and dir_fd == parent_fd:
            os.unlink(path, dir_fd=parent_fd)
            os.mkfifo(path, dir_fd=parent_fd)
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(workspace_files.os, "open", replace_with_fifo)
    try:
        with pytest.raises(WorkspaceBundleError):
            workspace_files._read_file(parent_fd, "item", 100)
    finally:
        os.close(parent_fd)


def test_directory_walk_stops_after_tree_entry_budget(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    consumed = []

    class Entries:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            consumed.append(len(consumed))
            return SimpleNamespace(name=f"entry-{len(consumed)}")

    monkeypatch.setattr(workspace_files.os, "scandir", lambda _fd: Entries())
    with pytest.raises(WorkspaceBundleError, match="too many entries"):
        pack_directory(root)
    assert len(consumed) == workspace_files._MAX_TREE_ENTRIES + 1


def test_pack_directory_rejects_special_files_and_hardlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    fifo = root / "pipe"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError):
        pytest.skip("named pipes are unavailable")
    with pytest.raises(WorkspaceBundleError):
        pack_directory(root)
    fifo.unlink()
    source = root / "source.txt"
    source.write_text("data")
    link = root / "hardlink.txt"
    try:
        os.link(source, link)
    except OSError:
        pytest.skip("hard links are unavailable")
    with pytest.raises(WorkspaceBundleError):
        pack_directory(root)


def test_restore_bundle_creates_fresh_private_tree_without_overwrite(tmp_path):
    destination = tmp_path / "task-workspace"
    restored = restore_bundle(
        _bundle([("sub/file.txt", b"hello"), ("root.txt", b"world")]),
        destination)

    assert restored == destination
    assert (destination / "sub" / "file.txt").read_bytes() == b"hello"
    assert stat.S_IMODE((destination / "root.txt").stat().st_mode) == 0o600
    with pytest.raises(WorkspaceBundleError):
        restore_bundle(_bundle([("root.txt", b"new")]), destination)
    assert (destination / "root.txt").read_bytes() == b"world"


def test_restore_bundle_rejects_existing_symlink_destination(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "workspace"
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceBundleError):
        restore_bundle(_bundle([("file.txt", b"data")]), destination)
    assert list(outside.iterdir()) == []


def test_restore_bundle_validates_before_creating_destination(tmp_path):
    destination = tmp_path / "bad"
    invalid = _bundle([("../escape", b"data")])
    with pytest.raises(WorkspaceBundleError):
        restore_bundle(invalid, destination)
    assert not destination.exists()


def test_failed_restore_does_not_delete_replacement_destination(tmp_path, monkeypatch):
    destination = tmp_path / "task-workspace"
    moved = tmp_path / "original-workspace"

    def replace_destination_then_fail(_parent_fd, _name, _content):
        destination.rename(moved)
        destination.mkdir()
        (destination / "keep.txt").write_text("owned elsewhere")
        raise OSError("injected write failure")

    monkeypatch.setattr(workspace_files, "_write_file", replace_destination_then_fail)
    with pytest.raises(WorkspaceBundleError, match="choose a new destination"):
        restore_bundle(_bundle([("file.txt", b"data")]), destination)
    assert (destination / "keep.txt").read_text() == "owned elsewhere"
    assert moved.is_dir() and list(moved.iterdir()) == []


def test_failed_restore_clears_files_but_retains_its_root(tmp_path, monkeypatch):
    destination = tmp_path / "task-workspace"
    real_write = workspace_files._write_file

    def write_then_fail(parent_fd, name, content):
        real_write(parent_fd, name, content)
        raise OSError("injected post-write failure")

    monkeypatch.setattr(workspace_files, "_write_file", write_then_fail)
    with pytest.raises(WorkspaceBundleError, match="choose a new destination"):
        restore_bundle(_bundle([("file.txt", b"data")]), destination)
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_restore_refuses_success_if_destination_is_replaced_after_write(
        tmp_path, monkeypatch):
    destination = tmp_path / "task-workspace"
    moved = tmp_path / "original-workspace"
    real_write = workspace_files._write_file

    def write_then_replace(parent_fd, name, content):
        real_write(parent_fd, name, content)
        destination.rename(moved)
        destination.mkdir()
        (destination / "keep.txt").write_text("replacement")

    monkeypatch.setattr(workspace_files, "_write_file", write_then_replace)
    with pytest.raises(WorkspaceBundleError, match="changed during restore"):
        restore_bundle(_bundle([("file.txt", b"data")]), destination)
    assert (destination / "keep.txt").read_text() == "replacement"
    assert moved.is_dir() and list(moved.iterdir()) == []


def test_restore_checks_identity_while_opening_created_root(tmp_path, monkeypatch):
    destination = tmp_path / "task-workspace"
    moved = tmp_path / "original-workspace"
    real_open = os.open

    def replace_before_open(path, flags, *args, dir_fd=None, **kwargs):
        if path == destination.name and dir_fd is not None:
            destination.rename(moved)
            destination.mkdir()
            (destination / "keep.txt").write_text("replacement")
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(workspace_files.os, "open", replace_before_open)
    with pytest.raises(WorkspaceBundleError, match="changed during creation"):
        restore_bundle(_bundle([("file.txt", b"data")]), destination)
    assert (destination / "keep.txt").read_text() == "replacement"
    assert moved.is_dir() and list(moved.iterdir()) == []
