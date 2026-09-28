"""Long runs must discover new local sessions without skipping final evidence."""

import json
import os
from pathlib import Path
import struct
import sys
import time

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
try:
    import tui_soak_support as SUPPORT
    import tui_soak_watch as WATCH
finally:
    sys.path.remove(str(TOOLS))


def _event(number):
    return {"event_id": f"evt_{number}", "seq": number, "session_id": f"ses_{number}",
            "run_id": "", "kind": "session.created", "payload": {}}


def _path(root, number):
    return root / "projects" / "workspace-fixture" / "tui" / "events" / ("a" * 24) / f"{number:024x}" / "events.jsonl"


def _write(path, number):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_event(number)) + "\n")


def _initial(tmp_path):
    root = tmp_path / "runtime"
    _write(_path(root, 1), 1)
    evidence = SUPPORT.JournalEvidence(root)
    evidence.poll()
    assert evidence.counts["session.created"] == 1
    return evidence, root


def _delay_scan(evidence):
    evidence._scan.close()
    evidence._scan = (None for _ in range(10000))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux notification contract")
def test_new_session_bypasses_sixty_thousand_historical_entries(tmp_path):
    baseline = len(list(Path("/proc/self/fd").iterdir()))
    evidence, root = _initial(tmp_path)
    try:
        assert len(evidence._hints.scopes) == 1
        history = root / "historical"
        history.mkdir()
        for number in range(60000):
            (history / str(number)).touch()
        evidence._scan.close()
        evidence._scan = evidence._entries()
        evidence.poll()  # Leave the bounded history scan inside the large old tree.
        fresh = _path(root, 2)
        fresh.parent.mkdir()
        evidence.poll()  # Directory notification arrives before its journal exists.
        assert fresh in evidence._hints.pending
        _write(fresh, 2)
        evidence.poll()
        assert evidence.counts["session.created"] == 2
        assert fresh not in evidence._hints.pending
        assert len(evidence.tails) <= 4 and len(evidence._hints.scopes) == 1
        assert len(list(Path("/proc/self/fd").iterdir())) - baseline <= 12
        evidence.finish(time.monotonic() + 10)
        assert evidence.summary()["files"] == 2
    finally:
        evidence.close()
    assert len(list(Path("/proc/self/fd").iterdir())) <= baseline


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux notification contract")
def test_watch_installation_backfills_a_session_created_before_snapshot(tmp_path, monkeypatch):
    original = WATCH.JournalHints._backfill

    def raced(self, scope, observed):
        _write(scope / f"{2:024x}" / "events.jsonl", 2)
        original(self, scope, observed)

    monkeypatch.setattr(WATCH.JournalHints, "_backfill", raced)
    evidence, _root = _initial(tmp_path)
    try:
        evidence.poll()
        assert evidence.counts["session.created"] == 2
        assert evidence.summary()["duplicate_ids"] == 0
    finally:
        evidence.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux notification contract")
@pytest.mark.parametrize("empty_file", [False, True])
def test_hint_survives_delayed_first_record(tmp_path, empty_file):
    evidence, root = _initial(tmp_path)
    fresh = _path(root, 2)
    try:
        fresh.parent.mkdir()
        if empty_file:
            fresh.touch()
        for _ in range(3):
            evidence.poll()
            assert fresh in evidence._hints.pending
            evidence.tails.pop(fresh, None)  # History may evict an empty candidate.
        _write(fresh, 2)
        evidence.poll()
        assert evidence.counts["session.created"] == 2
        assert not evidence._hints.pending
    finally:
        evidence.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux notification contract")
def test_empty_candidates_outlive_the_bounded_tail_cache(tmp_path):
    evidence, root = _initial(tmp_path)
    paths = [_path(root, number) for number in range(2, 7)]
    try:
        _delay_scan(evidence)
        for path in paths:
            path.parent.mkdir()
            path.touch()
        for _ in range(2):
            evidence.poll()
            assert len(evidence._hints.pending) == 5
            assert len(evidence.tails) == 4
        for number, path in enumerate(paths, 2):
            _write(path, number)
        evidence.poll()
        assert evidence.counts["session.created"] == 6
        assert not evidence._hints.pending
    finally:
        evidence.close()


def test_provider_paths_cannot_establish_a_local_scope_watch(tmp_path):
    hints = WATCH.JournalHints(tmp_path / "runtime", SUPPORT.SoakFailure)
    try:
        hints.observe(tmp_path / "provider" / "native-session" / "events.jsonl")
        hints.observe(tmp_path / "runtime" / "provider" / "session.jsonl")
        assert hints.fd == -1 and not hints.scopes
    finally:
        hints.close()


@pytest.mark.parametrize("mask", [0x4000, 0x400, 0x800, 0x8000])
def test_lost_notifications_or_scope_invalidation_fail_closed(tmp_path, mask):
    hints = WATCH.JournalHints(tmp_path, SUPPORT.SoakFailure)
    with pytest.raises(SUPPORT.SoakFailure, match="overflow or watched scope changed"):
        hints._consume(struct.pack("iIII", -1, mask, 0, 0))


def test_pending_hints_cannot_grow_with_history(tmp_path):
    hints = WATCH.JournalHints(tmp_path, SUPPORT.SoakFailure)
    for number in range(hints.MAX_PENDING):
        hints._hint(tmp_path, f"{number:024x}")
    with pytest.raises(SUPPORT.SoakFailure, match="pending-session limit"):
        hints._hint(tmp_path, f"{hints.MAX_PENDING:024x}")


def test_finish_consumes_all_unseen_files_and_all_batches(tmp_path):
    root = tmp_path / "runtime"
    journal = SUPPORT.JournalEvidence(root)
    path = root / "previously-unseen" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(_event(number)) + "\n" for number in range(1, 401)))
    try:
        _delay_scan(journal)
        journal.poll()
        assert journal.summary()["files"] == 0
        journal.finish(time.monotonic() + 5)
        assert journal.summary()["events"] == 400
        assert journal.summary()["bytes"] == path.stat().st_size
    finally:
        journal.close()


@pytest.mark.parametrize("contents", [b"not-json\n", b'{"partial":', b"x" * (1024 * 1024 + 1)])
def test_finish_rejects_bad_evidence_that_poll_never_discovered(tmp_path, contents):
    journal = SUPPORT.JournalEvidence(tmp_path / "runtime")
    path = tmp_path / "runtime" / "unseen" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(contents)
    try:
        _delay_scan(journal)
        journal.poll()
        assert journal.summary()["files"] == 0
        with pytest.raises(SUPPORT.SoakFailure):
            journal.finish(time.monotonic() + 5)
    finally:
        journal.close()


def test_finish_rejects_disappeared_previously_read_journal(tmp_path):
    journal, root = _initial(tmp_path)
    _path(root, 1).unlink()
    try:
        with pytest.raises(SUPPORT.SoakFailure, match="disappeared"):
            journal.finish(time.monotonic() + 5)
    finally:
        journal.close()


def test_history_depth_is_bounded_and_scan_descriptors_close_on_failure(tmp_path):
    root = tmp_path / "runtime"
    root.joinpath(*["nested"] * 34).mkdir(parents=True)
    journal = SUPPORT.JournalEvidence(root)
    try:
        with pytest.raises(SUPPORT.SoakFailure, match="directory-depth limit"):
            journal.finish(time.monotonic() + 5)
    finally:
        journal.close()


def test_finish_keeps_its_original_deadline(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "entry").touch()
    journal = SUPPORT.JournalEvidence(root)
    try:
        with pytest.raises(SUPPORT.SoakFailure, match="deadline"):
            journal.finish(time.monotonic() - 1)
    finally:
        journal.close()
