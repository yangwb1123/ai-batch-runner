"""Bounded compaction tests for durable Fabric task snapshots."""

from __future__ import annotations

import pytest

from pbatch import fabric_task_store
from pbatch.fabric_task_store import TaskStateError, TaskStateStore


def test_compact_over_file_limit_preserves_old_state_and_cleans_temp(
        tmp_path, monkeypatch):
    store = TaskStateStore(tmp_path)
    store.save({"task_id": "old"})
    before = store.path.read_bytes()
    monkeypatch.setattr(fabric_task_store, "MAX_FILE_BYTES", 100)
    snapshots = {
        task_id: {"task": {"task_id": task_id}, "lease": None}
        for task_id in ("first", "second")
    }

    with pytest.raises(TaskStateError, match="exceeds file limit"):
        store.compact(snapshots)

    assert store.path.read_bytes() == before
    assert not store.path.with_name(store.path.name + ".tmp").exists()
