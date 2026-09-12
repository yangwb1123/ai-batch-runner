"""Private command-output ArtifactRef storage contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

from pbatch.tui_command_artifacts import (
    COMMAND_ARTIFACT_MARKER,
    CommandArtifactContext,
    CommandArtifactStore,
)


def _context(item: str = "item-1") -> CommandArtifactContext:
    return CommandArtifactContext(
        workspace_id="workspace-secret-name",
        session_id="session-secret-name",
        run_id="run-secret-name",
        turn_id="turn-secret-name",
        item_id=item,
    )


def _store(tmp_path: Path, **changes) -> CommandArtifactStore:
    values = {
        "preview_bytes": 16,
        "max_bytes": 128,
        "store_max_bytes": 512,
        "max_files": 4,
    }
    values.update(changes)
    return CommandArtifactStore(str(tmp_path), **values)


def _artifact_path(store: CommandArtifactStore, relative: str) -> Path:
    return store.data_root / relative


def test_preview_sized_output_does_not_create_artifact(tmp_path):
    store = _store(tmp_path)
    outcome = store.capture(_context(), "short output")

    assert outcome.artifact is None and outcome.warning == ""
    assert not list(store.data_root.rglob("*.command-output.txt"))
    store.close()


def test_oversized_output_is_private_hashed_and_event_ready(tmp_path):
    store = _store(tmp_path, max_bytes=512, store_max_bytes=1024)
    content = "hello 世界\n" * 8
    outcome = store.capture(_context(), content)

    assert outcome.warning == "" and outcome.artifact is not None
    artifact = outcome.artifact
    path = _artifact_path(store, artifact.path)
    assert path.read_text(encoding="utf-8") == content
    assert stat_mode(path) == 0o600
    assert stat_mode(path.parent) == 0o700
    assert artifact.original_bytes == len(content.encode("utf-8"))
    assert artifact.size_bytes == artifact.original_bytes
    assert not artifact.truncated
    assert all(secret not in artifact.path for secret in (
        "workspace-secret-name", "session-secret-name", "run-secret-name",
        "turn-secret-name", "item-1"))

    payload = artifact.event_payload("raw:item-1", "Command output · pytest")
    assert payload["scope"] == "data" and payload["kind"] == "command_output"
    assert payload["path"] == artifact.path and payload["sha256"] == artifact.sha256
    assert payload["name"] == "Command output · pytest"
    store.close()


def test_artifact_cap_preserves_utf8_and_marks_truncation(tmp_path):
    store = _store(tmp_path, max_bytes=96, store_max_bytes=192)
    content = "界" * 100
    outcome = store.capture(_context(), content)

    artifact = outcome.artifact
    assert artifact is not None and artifact.truncated
    path = _artifact_path(store, artifact.path)
    data = path.read_bytes()
    assert len(data) <= 96 and data.endswith(COMMAND_ARTIFACT_MARKER)
    assert data.decode("utf-8").endswith(COMMAND_ARTIFACT_MARKER.decode())
    assert artifact.original_bytes == len(content.encode("utf-8"))
    assert artifact.size_bytes == len(data)
    store.close()


def test_streamed_output_promotes_after_preview_boundary(tmp_path):
    store = _store(tmp_path, max_bytes=512, store_max_bytes=1024)
    context = _context()
    for chunk in ("first-", "second-", "third-and-oversized"):
        store.append(context, chunk)
    outcome = store.finish(context)

    assert outcome.artifact is not None
    assert _artifact_path(store, outcome.artifact.path).read_text() == (
        "first-second-third-and-oversized")
    store.close()


def test_retention_replaces_oldest_within_file_budget(tmp_path):
    store = _store(tmp_path, max_files=1)
    first = store.capture(_context("item-1"), "a" * 32).artifact
    second = store.capture(_context("item-2"), "b" * 32).artifact

    assert first is not None and second is not None
    assert not _artifact_path(store, first.path).exists()
    assert _artifact_path(store, second.path).read_text() == "b" * 32
    assert len(list(store.data_root.rglob("*.command-output.txt"))) == 1
    store.close()


def test_symlinked_artifact_directory_fails_without_writing_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    from pbatch.user_dirs import project_runtime_dir
    data_root = project_runtime_dir(str(tmp_path))
    data_root.mkdir(parents=True)
    (data_root / "tui").symlink_to(outside, target_is_directory=True)
    store = _store(tmp_path)

    outcome = store.capture(_context(), "x" * 32)

    assert outcome.artifact is None and "symlink" in outcome.warning
    assert list(outside.iterdir()) == []
    store.close()


def test_active_limit_reports_once_at_finish_and_close_removes_temp(tmp_path):
    store = _store(tmp_path, max_active=1)
    first, second = _context("first"), _context("second")
    store.append(first, "small")
    store.append(second, "x" * 32)

    assert "active limit" in store.finish(second).warning
    store.append(first, "y" * 32)
    assert list(store.data_root.rglob("*.tmp"))
    store.close()
    assert not list(store.data_root.rglob("*.tmp"))


def test_two_store_instances_serialize_retention(tmp_path):
    stores = [_store(tmp_path, max_files=2), _store(tmp_path, max_files=2)]

    def capture(index):
        return stores[index].capture(
            _context(f"parallel-{index}"), str(index) * 40)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(capture, (0, 1)))

    assert all(outcome.artifact is not None for outcome in outcomes)
    assert len(list(stores[0].data_root.rglob("*.command-output.txt"))) == 2
    for store in stores:
        store.close()


def test_constructor_rejects_incoherent_capacity(tmp_path):
    try:
        _store(tmp_path, max_bytes=48)
    except ValueError as exc:
        assert "max_bytes" in str(exc)
    else:
        raise AssertionError("accepted max_bytes without marker headroom")


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
