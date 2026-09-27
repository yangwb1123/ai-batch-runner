"""Failed command artifact allocation cannot leak file or directory descriptors."""
from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from pbatch import tui_command_artifacts as artifacts


pytestmark = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="POSIX command artifact storage")


def _context(index=0):
    return artifacts.CommandArtifactContext("workspace", "session", "run", "turn", str(index))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    value = artifacts.CommandArtifactStore(str(tmp_path), preview_bytes=8, max_bytes=96,
                                           store_max_bytes=192)
    yield value
    value.close()


def _assert_closed(descriptor):
    with pytest.raises(OSError) as caught:
        os.fstat(descriptor)
    assert caught.value.errno == errno.EBADF


def test_failed_temp_allocation_releases_directory_fd_and_next_capture_recovers(store, monkeypatch):
    original_open_directory = store._open_artifact_directory
    opened = []

    def track_directory(context):
        descriptor, relative = original_open_directory(context)
        opened.append(descriptor)
        return descriptor, relative

    def reject_allocation(_directory, _name):
        raise PermissionError("temporary creation denied")

    monkeypatch.setattr(store, "_open_artifact_directory", track_directory)
    with monkeypatch.context() as failure:
        failure.setattr(artifacts, "_create_private_temp", reject_allocation)
        for index in range(5):
            outcome = store.capture(_context(index), "x" * 32)
            assert outcome.artifact is None and "denied" in outcome.warning
            _assert_closed(opened[-1])
            assert not store._captures and not store._failures
    assert not list(store.data_root.rglob("*.tmp"))
    recovered = store.capture(_context(99), "recovered output")
    assert recovered.artifact is not None and not recovered.warning
    assert (store.data_root / recovered.artifact.path).read_text() == "recovered output"


def test_fchmod_failure_closes_new_temp_fd_and_removes_unpublished_file(store, monkeypatch):
    created_descriptors = []

    def reject_mode(descriptor, _mode):
        created_descriptors.append(descriptor)
        raise OSError(errno.EIO, "temporary permissions failed")

    with monkeypatch.context() as failure:
        failure.setattr(artifacts.os, "fchmod", reject_mode)
        for index in range(3):
            outcome = store.capture(_context(index), "x" * 32)
            assert outcome.artifact is None and "permissions failed" in outcome.warning
            _assert_closed(created_descriptors[-1])
            assert not list(store.data_root.rglob("*.tmp"))
    assert len(created_descriptors) == 3
    recovered = store.capture(_context(99), "recovered output")
    assert recovered.artifact is not None and not recovered.warning


def test_exhausted_unique_temp_names_release_directory_without_deleting_foreign_file(
        store, monkeypatch):
    directory, relative = store._open_artifact_directory(_context())
    os.close(directory)
    final_name = artifacts._artifact_name(_context())
    existing = store.data_root / relative / f".{final_name}.collision.tmp"
    existing.write_bytes(b"another capture owns this temporary")
    opened = []
    original = store._open_artifact_directory

    def track(context):
        descriptor, value = original(context)
        opened.append(descriptor)
        return descriptor, value

    monkeypatch.setattr(store, "_open_artifact_directory", track)
    monkeypatch.setattr(artifacts.secrets, "token_hex", lambda _count: "collision")
    outcome = store.capture(_context(), "x" * 32)
    assert outcome.artifact is None and "unique" in outcome.warning
    _assert_closed(opened[-1])
    assert existing.read_bytes() == b"another capture owns this temporary"
