"""Private state does not follow links, block on FIFOs, or interleave local writers."""
from __future__ import annotations

import os
import threading
import pytest

from pbatch import task_state_io as io
from pbatch.task_center_io import read_rows
from pbatch.task_goal_view import EvidenceWindow
from pbatch.task_goal_store import evidence_reference


def test_concurrent_local_appends_are_complete_and_unique(tmp_path):
    path = tmp_path / "events.jsonl"
    errors = []

    def writer(index):
        try:
            for number in range(8):
                io.append_bounded(path, {"writer": index, "number": number})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors and not any(thread.is_alive() for thread in threads)
    rows, info = read_rows(path)
    assert len(rows) == 32 and len({(row["writer"], row["number"]) for row in rows}) == 32
    assert not info["malformed"] and not info["incomplete"]


def test_symlink_lock_does_not_touch_its_target(tmp_path):
    path = tmp_path / "state.json"
    target = tmp_path / "secret"
    target.write_text("preserve")
    path.with_suffix(".json.lock").symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        with io.locked(path):
            io.atomic_write(path, {"value": "wrong"})
    assert target.read_text() == "preserve" and not path.exists()


def test_fifo_ledger_is_refused_without_opening_a_blocking_writer(tmp_path):
    path = tmp_path / "pipe.jsonl"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        io.append_bounded(path, {"value": "wrong"})


def test_over_budget_atomic_write_preserves_old_state_and_removes_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    with io.locked(path):
        io.atomic_write(path, {"value": "old"})
    previous = path.read_bytes()
    monkeypatch.setattr(io, "MAX_STATE_BYTES", 20)
    with pytest.raises(ValueError, match="byte budget"):
        with io.locked(path):
            io.atomic_write(path, {"value": "x" * 200})
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob(".task-state-*"))


def test_ledger_rotation_is_bounded_and_leaves_complete_newest_records(tmp_path, monkeypatch):
    monkeypatch.setattr(io, "MAX_LEDGER_BYTES", 128)
    path = tmp_path / "events.jsonl"
    for index in range(30):
        io.append_bounded(path, {"index": index})
    rows, info = read_rows(path)
    assert path.stat().st_size <= 128 and rows[-1]["index"] == 29
    assert not info["malformed"] and not info["incomplete"]


def test_changed_during_hash_still_consumes_query_budget(tmp_path, monkeypatch):
    from pbatch import task_goal_store
    root = tmp_path / "project"
    root.mkdir()
    file = root / "proof"
    file.write_text("bound evidence")
    source = {"source_id": "demo", "data_dir": str(root), "workspace": str(root)}
    reference = evidence_reference(source, str(file))
    original = task_goal_store._stat_key
    calls = []

    def unstable(info):
        calls.append(info)
        return (*original(info), len(calls))

    monkeypatch.setattr(task_goal_store, "_stat_key", unstable)
    window = EvidenceWindow()
    before = window.remaining
    assert window.verify(source, reference) == "unavailable"
    assert window.remaining == before - len("bound evidence")
