"""Crash recovery and trust-boundary contracts for the TUI event journal."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
import queue
import subprocess
import sys
import time

import pytest

from pbatch import config
from pbatch.tui_controller import TUIEventController
from pbatch.tui_events import EventBus
import pbatch.tui_journal as tui_journal
from pbatch.tui_journal import RuntimeEventJournal, RuntimeEventJournalError
from pbatch.tui_projection_ui import TUIProjectionMixin
from pbatch.tui_protocol import MAX_EVENT_LINE_BYTES, RuntimeEvent, TurnState


@pytest.fixture
def journal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime-data"))
    return tmp_path


def _event(seq=1, kind="turn.started", *, session="ses_journal",
           run="run_one", turn="turn_one", **changes):
    values = {
        "event_id": f"evt_{run}_{seq}_{kind.replace('.', '_')}",
        "seq": seq,
        "occurred_at": f"2026-08-23T12:00:{seq:02d}Z",
        "workspace_id": "ws_one",
        "session_id": session,
        "run_id": run,
        "turn_id": turn,
        "actor_id": "agent_main",
        "kind": kind,
        "payload": {},
    }
    values.update(changes)
    return RuntimeEvent(**values)


def test_round_trip_uses_hashed_scope_and_redacts_persisted_secrets(journal_env):
    session = "session/../../not-a-path"
    journal = RuntimeEventJournal(str(journal_env), session)
    event = _event(
        kind="message.completed", session=session,
        payload={"item_id": "message-1", "text": "api_key=super-secret"})

    assert journal.append(event) is True
    recovered = journal.recover()

    assert not recovered.degraded and len(recovered.events) == 1
    assert recovered.events[0].payload["text"] == "api_key=[REDACTED]"
    assert session not in str(journal.path)
    assert journal.path.is_relative_to(journal_env / "runtime-data")
    assert stat_mode(journal.path) == 0o600
    assert stat_mode(journal.meta_path) == 0o600
    assert stat_mode(journal.lock_path) == 0o600
    assert stat_mode(journal.directory) == 0o700


def test_controller_writes_and_revalidates_projection_checkpoint(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    controller = TUIEventController(journal=journal)

    controller.ingest(_event())
    controller.ingest(_event(
        seq=2, kind="turn.completed", payload={"state": "SUCCEEDED"}))

    assert journal.checkpoint_path.is_file()
    assert controller.checkpoint_status == "written"
    controller.close()

    rebuilt = TUIEventController(
        journal=RuntimeEventJournal(str(journal_env), "ses_journal"))
    assert rebuilt.checkpoint_status == "verified"
    assert rebuilt.recovery.read_only is False


def test_invalid_projection_checkpoint_is_ignored_fail_closed(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    controller = TUIEventController(journal=journal)
    controller.ingest(_event())
    controller.ingest(_event(
        seq=2, kind="turn.completed", payload={"state": "SUCCEEDED"}))
    controller.close()

    value = json.loads(journal.checkpoint_path.read_text())
    value["projection_digest"] = "0" * 64
    journal.checkpoint_path.write_text(json.dumps(value))

    rebuilt = TUIEventController(
        journal=RuntimeEventJournal(str(journal_env), "ses_journal"))
    assert rebuilt.checkpoint_status == "mismatch"
    assert rebuilt.recovery.read_only is False


def test_duplicate_event_is_not_appended_twice(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    event = _event()

    assert journal.append(event) is True
    size = journal.path.stat().st_size
    assert journal.append(event) is False

    assert journal.path.stat().st_size == size
    assert len(journal.recover().events) == 1


def test_archive_marker_blocks_append_and_restore_preserves_history(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    first = _event()
    assert journal.append(first)
    original = journal.path.read_bytes()

    assert journal.mark_archived() is True
    assert journal.mark_archived() is False
    assert journal.archived is True
    with pytest.raises(RuntimeEventJournalError, match="journal is archived"):
        journal.append(_event(2, "turn.completed"))
    assert journal.path.read_bytes() == original
    assert journal.recover().events == (first,)

    assert journal.restore_archived() is True
    assert journal.restore_archived() is False
    assert journal.archived is False
    assert journal.append(_event(2, "turn.completed"))


def test_invalid_archive_marker_fails_closed(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    journal.recover()
    journal.archive_path.write_text('{"schema_version":1}\n', encoding="utf-8")

    with pytest.raises(RuntimeEventJournalError, match="archive marker is invalid"):
        _ = journal.archived
    with pytest.raises(RuntimeEventJournalError, match="archive marker is invalid"):
        journal.append(_event())


def test_archive_compare_and_mark_rejects_a_changed_journal(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event())

    with pytest.raises(RuntimeEventJournalError, match="changed while preparing"):
        journal.mark_archived(expected_bytes=0)
    assert not journal.archive_path.exists()
    assert journal.archived is False


def test_archive_marker_symlink_is_never_followed(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    journal.recover()
    outside = journal_env / "outside-archive.json"
    outside.write_text('{}\n', encoding="utf-8")
    journal.archive_path.symlink_to(outside)

    with pytest.raises(RuntimeEventJournalError, match="symlink journal metadata"):
        _ = journal.archived
    assert outside.read_text(encoding="utf-8") == '{}\n'


def test_non_monotonic_direct_append_is_rejected_without_growth(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event(2))
    size = journal.path.stat().st_size

    with pytest.raises(RuntimeEventJournalError, match="non-monotonic"):
        journal.append(_event(1, event_id="evt_late"))
    assert journal.path.stat().st_size == size


def test_file_cap_is_checked_at_the_exact_append_boundary(journal_env):
    first = _event()
    limit = len(first.to_jsonl().encode("utf-8"))
    journal = RuntimeEventJournal(
        str(journal_env), "ses_journal", max_bytes=limit)

    assert journal.append(first)
    with pytest.raises(RuntimeEventJournalError, match="capacity exhausted"):
        journal.append(_event(2, "usage.updated", turn=""))

    assert journal.path.stat().st_size == limit


def test_crash_truncated_tail_is_removed_before_append_resumes(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    first = _event()
    second = _event(2, "message.delta", payload={"text": "safe"})
    assert journal.append(first)
    trusted_size = journal.path.stat().st_size
    with journal.path.open("ab") as handle:
        handle.write(b'{"partial":')

    reopened = RuntimeEventJournal(str(journal_env), "ses_journal")
    recovery = reopened.recover()

    assert recovery.truncated_tail and not recovery.degraded
    assert recovery.trusted_bytes == trusted_size
    assert reopened.path.stat().st_size == trusted_size
    assert reopened.append(second)
    assert [item.event_id for item in reopened.recover().events] == [
        first.event_id, second.event_id]


def test_complete_corrupt_record_stops_at_prefix_and_blocks_writes(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    first = _event()
    assert journal.append(first)
    with journal.path.open("ab") as handle:
        handle.write(b'{"bad":true}\n')
    damaged_size = journal.path.stat().st_size

    reopened = RuntimeEventJournal(str(journal_env), "ses_journal")
    recovery = reopened.recover()

    assert recovery.degraded and not recovery.truncated_tail
    assert [item.event_id for item in recovery.events] == [first.event_id]
    assert "untrusted journal line 2" in recovery.warnings[0]
    with pytest.raises(RuntimeEventJournalError, match="degraded and read-only"):
        reopened.append(_event(2, "message.delta", payload={"text": "later"}))
    assert reopened.path.stat().st_size == damaged_size


def test_valid_suffix_after_corruption_is_never_recovered(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    first = _event()
    suffix = _event(2, "usage.updated", turn="")
    assert journal.append(first)
    with journal.path.open("ab") as handle:
        handle.write(b'{"bad":true}\n')
        handle.write(suffix.to_jsonl().encode("utf-8"))
    damaged = journal.path.read_bytes()

    recovery = RuntimeEventJournal(
        str(journal_env), "ses_journal").recover()

    assert recovery.degraded
    assert [event.event_id for event in recovery.events] == [first.event_id]
    assert journal.path.read_bytes() == damaged


def test_oversized_complete_record_is_bounded_and_degrades(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    journal.recover()
    with journal.path.open("wb") as handle:
        handle.write(b"x" * (MAX_EVENT_LINE_BYTES + 1) + b"\n")

    recovery = RuntimeEventJournal(
        str(journal_env), "ses_journal").recover()

    assert recovery.degraded and recovery.events == ()
    assert "record exceeds line limit" in recovery.warnings[0]


def test_symlink_journal_target_is_rejected(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    journal.recover()
    target = journal_env / "outside.jsonl"
    target.write_text("untouched", encoding="utf-8")
    journal.path.symlink_to(target)

    with pytest.raises(RuntimeEventJournalError, match="symlink journal path"):
        journal.append(_event())
    assert target.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("field", [
    "schema_version", "tenant", "project_id", "sensitivity", "session_id",
    "cwd_id",
])
def test_metadata_boundary_tampering_is_rejected_without_mutation(
        journal_env, field):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event())
    metadata = json.loads(journal.meta_path.read_text(encoding="utf-8"))
    if field in metadata["scope"]:
        metadata["scope"][field] = "tampered"
    else:
        metadata[field] = "tampered"
    journal.meta_path.write_text(
        json.dumps(metadata) + "\n", encoding="utf-8")
    original = journal.path.read_bytes()

    reopened = RuntimeEventJournal(str(journal_env), "ses_journal")
    with pytest.raises(RuntimeEventJournalError, match="boundary mismatch"):
        reopened.recover()
    assert journal.path.read_bytes() == original


def test_metadata_symlink_is_rejected_without_following_target(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    journal.recover()
    outside = journal_env / "outside-meta.json"
    outside.write_text("{}\n", encoding="utf-8")
    journal.meta_path.unlink()
    journal.meta_path.symlink_to(outside)

    with pytest.raises(RuntimeEventJournalError, match="symlink journal metadata"):
        journal.recover()
    assert outside.read_text(encoding="utf-8") == "{}\n"


@pytest.mark.parametrize("name,value", [
    ("PBATCH_MEMORY_TENANT", "tenant-after-bind"),
    ("PBATCH_MEMORY_SENSITIVITY", "secret"),
])
def test_scope_drift_after_binding_refuses_old_journal(
        journal_env, monkeypatch, name, value):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event())
    original = journal.path.read_bytes()
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeEventJournalError, match="scope changed"):
        journal.append(_event(2, "usage.updated", turn=""))
    assert journal.path.read_bytes() == original


def test_data_directory_drift_after_binding_refuses_old_journal(
        journal_env, monkeypatch):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event())
    original = journal.path.read_bytes()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(journal_env / "other-data"))

    with pytest.raises(RuntimeEventJournalError, match="data directory changed"):
        journal.append(_event(2, "usage.updated", turn=""))
    assert journal.path.read_bytes() == original


def test_two_writers_serialize_and_refresh_their_indexes(journal_env):
    first = RuntimeEventJournal(str(journal_env), "ses_journal")
    second = RuntimeEventJournal(str(journal_env), "ses_journal")
    events = [
        _event(run="run_a"),
        _event(run="run_b", event_id="evt_other_run"),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda pair: pair[0].append(pair[1]),
            ((first, events[0]), (second, events[1]))))

    assert results == [True, True]
    recovered = RuntimeEventJournal(
        str(journal_env), "ses_journal").recover()
    assert {event.event_id for event in recovered.events} == {
        "evt_run_a_1_turn_started", "evt_other_run"}


def test_two_writers_append_the_same_event_exactly_once(journal_env):
    writers = [RuntimeEventJournal(str(journal_env), "ses_journal")
               for _index in range(2)]
    event = _event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda writer: writer.append(event), writers))

    assert sorted(results) == [False, True]
    assert len(RuntimeEventJournal(
        str(journal_env), "ses_journal").recover().events) == 1


def test_two_processes_append_the_same_event_exactly_once(journal_env):
    script = (
        "import os,sys,time; from pathlib import Path; "
        "import pbatch.tui_journal as journal_module; "
        "from pbatch.tui_journal import RuntimeEventJournal; "
        "from pbatch.tui_protocol import RuntimeEvent; "
        "event=RuntimeEvent.from_jsonl(sys.argv[3]); "
        "barrier=Path(sys.argv[4]); (barrier/('ready-'+sys.argv[5])).touch(); "
        "deadline=time.monotonic()+5; "
        "exec('while not (barrier/\"start\").exists():\\n"
        " if time.monotonic()>=deadline: raise TimeoutError(\"barrier\")\\n"
        " time.sleep(0.01)'); "
        "journal_module._fsync_file=lambda fd:(time.sleep(0.2),os.fsync(fd))[-1]; "
        "print(int(RuntimeEventJournal(sys.argv[1],sys.argv[2]).append(event)))"
    )
    barrier = journal_env / "process-barrier"
    barrier.mkdir()
    base = [sys.executable, "-c", script, str(journal_env),
            "ses_journal", _event().to_jsonl(), str(barrier)]
    processes = [subprocess.Popen(
        [*base, str(index)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True) for index in range(2)]
    deadline = time.monotonic() + 5
    while len(list(barrier.glob("ready-*"))) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    (barrier / "start").touch()
    outputs = [process.communicate(timeout=10) for process in processes]

    assert all(process.returncode == 0 for process in processes), outputs
    assert sorted(stdout.strip() for stdout, _stderr in outputs) == ["0", "1"]
    assert len(RuntimeEventJournal(
        str(journal_env), "ses_journal").recover().events) == 1


def test_recovery_enforces_run_index_limit(journal_env, monkeypatch):
    monkeypatch.setattr(tui_journal, "MAX_JOURNAL_RUNS", 2)
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event(run="run_a"))
    assert journal.append(_event(run="run_b", event_id="evt_run_b"))
    third = _event(run="run_c", event_id="evt_run_c")
    with journal.path.open("ab") as handle:
        handle.write(third.to_jsonl().encode("utf-8"))

    recovery = RuntimeEventJournal(
        str(journal_env), "ses_journal").recover()

    assert recovery.degraded and len(recovery.events) == 2
    assert "run limit exceeded" in recovery.warnings[0]


class _FlakyJournal:
    def __init__(self):
        self.fail = True
        self.events = []

    def append(self, event):
        if self.fail:
            raise RuntimeError("fsync failed")
        self.events.append(event)
        return True


def test_event_bus_is_durable_before_visible_and_retryable():
    journal = _FlakyJournal()
    bus = EventBus(journal=journal)
    event = _event(seq=0, event_id="evt_auto")

    with pytest.raises(RuntimeError, match="fsync failed"):
        bus.publish(event)
    assert event.seq == 0
    with pytest.raises(queue.Empty):
        bus.get(timeout=0)

    journal.fail = False
    assert bus.publish(event) is True and event.seq == 1
    assert bus.get(timeout=0).event_id == event.event_id
    assert [item.event_id for item in journal.events] == [event.event_id]


def test_event_bus_journals_a_canonical_copy_before_producer_mutation(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    bus = EventBus(journal=journal)
    event = _event(payload={"nested": {"value": "original"}})

    assert bus.publish(event)
    event.payload["nested"]["value"] = "mutated"

    recovered = RuntimeEventJournal(
        str(journal_env), "ses_journal").recover()
    assert recovered.events[0].payload["nested"]["value"] == "original"


def test_fsync_failure_rolls_back_and_retry_becomes_visible(
        journal_env, monkeypatch):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    controller = TUIEventController(journal=journal)
    event = _event(seq=0, event_id="evt_retry")
    original_fsync = tui_journal._fsync_file
    attempts = 0

    def fail_once(descriptor):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(tui_journal, "_fsync_file", fail_once)
    with pytest.raises(RuntimeEventJournalError, match="cannot append"):
        controller.ingest(event)
    assert journal.path.stat().st_size == 0
    assert controller.projection.turns == {}
    with pytest.raises(queue.Empty):
        controller.bus.get(timeout=0)

    assert controller.ingest(event) is not None
    assert event.seq == 1
    assert len(journal.recover().events) == 1


def test_ambiguous_failed_rollback_stays_read_only(
        journal_env, monkeypatch):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    event = _event(seq=1, event_id="evt_ambiguous")
    monkeypatch.setattr(
        tui_journal, "_fsync_file",
        lambda _descriptor: (_ for _item in ()).throw(OSError("fsync failed")))
    monkeypatch.setattr(tui_journal, "_rollback_append", lambda *_args: False)

    with pytest.raises(RuntimeEventJournalError) as failure:
        journal.append(event)
    assert failure.value.retryable is False
    assert journal.path.stat().st_size > 0
    with pytest.raises(RuntimeEventJournalError, match="degraded and read-only"):
        journal.append(event)
    assert journal.recover().degraded


def test_partial_write_is_rolled_back_before_retry(journal_env, monkeypatch):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    original_write = tui_journal._write_once

    def partial(descriptor, value):
        return original_write(descriptor, value[:-1])

    monkeypatch.setattr(tui_journal, "_write_once", partial)
    with pytest.raises(RuntimeEventJournalError) as failure:
        journal.append(_event())
    assert failure.value.retryable is True
    assert journal.path.stat().st_size == 0

    monkeypatch.setattr(tui_journal, "_write_once", original_write)
    assert journal.append(_event())


def test_directory_fsync_failure_is_fail_closed(journal_env, monkeypatch):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    original_fsync = tui_journal._fsync_directory
    calls = []
    failed = False

    def fail_directory(path):
        nonlocal failed
        calls.append(path)
        if not failed:
            failed = True
            raise RuntimeEventJournalError("simulated directory fsync failure")
        original_fsync(path)

    monkeypatch.setattr(tui_journal, "_fsync_directory", fail_directory)
    with pytest.raises(RuntimeEventJournalError, match="directory fsync failure"):
        journal.append(_event())
    assert not journal.path.exists()

    calls.clear()
    assert journal.append(_event())
    assert journal.data_root.parent in calls


def test_secret_identity_redaction_stays_distinct_and_bounded(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    controller = TUIEventController(journal=journal)
    for event in (
            _event(1, "turn.started"),
            _event(2, "message.completed", payload={
                "item_id": "token=qz", "text": "first"}),
            _event(3, "message.completed", payload={
                "item_id": "token=-I", "text": "second"}),
            _event(4, "turn.completed")):
        controller.ingest(event)

    recovery = RuntimeEventJournal(
        str(journal_env), "ses_journal").recover()
    item_ids = [event.payload["item_id"] for event in recovery.events
                if event.kind == "message.completed"]
    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_env), "ses_journal"))

    assert len(set(item_ids)) == 2
    assert all("token" not in item_id for item_id in item_ids)
    assert rebuilt.snapshot().assistant_text == "first\n\nsecond"


def test_redaction_never_expands_a_valid_event_past_protocol_limits(journal_env):
    items = [{"item_id": f"api_key={index}"} for index in range(1_800)]
    event = _event(kind="runtime.warning", turn="", payload={"items": items})
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")

    assert journal.append(event)
    recovered = journal.recover().events[0]
    assert len(recovered.to_jsonl().encode("utf-8")) <= MAX_EVENT_LINE_BYTES
    assert recovered.payload["journal_payload_truncated"] is True
    assert "items" not in recovered.payload
    assert "payload compacted" in journal.recovery.warnings[-1]


def test_secret_like_envelope_identity_is_rejected_before_disk(journal_env):
    with pytest.raises(RuntimeEventJournalError, match="secret-like"):
        RuntimeEventJournal(str(journal_env), "api_key=session-secret")
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    with pytest.raises(RuntimeEventJournalError, match="actor_id"):
        journal.append(_event(actor_id="api_key=actor-secret"))
    assert not journal.path.exists()


def test_oversized_existing_file_is_read_only_without_prefix_scan(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event())
    size = journal.path.stat().st_size

    recovery = RuntimeEventJournal(
        str(journal_env), "ses_journal", max_bytes=size - 1).recover()

    assert recovery.degraded and recovery.events == ()
    assert recovery.total_bytes == size
    assert recovery.trusted_bytes == 0
    assert "exceeds configured file limit" in recovery.warnings[0]


def test_controller_rebuild_restores_last_turn_without_republishing(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    live = TUIEventController(journal=journal)
    events = [
        _event(1, "turn.started"),
        _event(2, "message.delta", payload={"item_id": "m1", "text": "old"}),
        _event(3, "turn.completed"),
        _event(1, "turn.started", run="run_two", turn="turn_two"),
        _event(2, "message.started", run="run_two", turn="turn_two",
               payload={"item_id": "m2"}),
        _event(3, "message.delta", run="run_two", turn="turn_two",
               payload={"item_id": "m2", "text": "draft"}),
        _event(4, "plan.updated", run="run_two", turn="turn_two",
               payload={"item_id": "plan", "items": [
                   {"text": "inspect", "completed": True}]}),
        _event(5, "workspace.snapshot", run="run_two", turn="turn_two",
               payload={"phase": "after", "workspace": {
                   "available": True, "root": "/repo", "branch": "main"},
                   "delta": {"changed": True, "paths": ["new.py"]}},
               extensions={"runtime": "test", "authoritative": True}),
        _event(6, "message.completed", run="run_two", turn="turn_two",
               payload={"item_id": "m2", "text": "final"}),
        _event(7, "turn.completed", run="run_two", turn="turn_two"),
    ]
    for event in events:
        live.ingest(event)
    expected = live.snapshot()
    size = journal.path.stat().st_size

    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_env), "ses_journal"))

    assert replace(rebuilt.snapshot(), revision=0) == replace(expected, revision=0)
    assert rebuilt.snapshot().assistant_text == "final"
    assert rebuilt.snapshot().timeline.summary == expected.timeline.summary
    assert rebuilt.snapshot().workspace.paths == ("new.py",)
    with pytest.raises(queue.Empty):
        rebuilt.bus.get(timeout=0)
    assert rebuilt.ingest(events[-1]) is None
    assert journal.path.stat().st_size == size


def test_live_projection_invalid_event_is_rejected_before_journaling(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    controller = TUIEventController(journal=journal)
    invalid = _event(kind="turn.completed")

    with pytest.raises(ValueError, match="turn must begin"):
        controller.ingest(invalid)
    assert not journal.path.exists() or journal.path.stat().st_size == 0
    assert controller.ingest(_event(event_id="evt_valid_start")) is not None


def test_projection_invalid_persisted_event_degrades_rebuild(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event(kind="turn.completed"))

    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_env), "ses_journal"))

    assert rebuilt.recovery.degraded and rebuilt.recovery.read_only
    assert rebuilt.recovery.applied_events == 0
    assert "projection rebuild stopped" in rebuilt.recovery.warnings[-1]
    assert journal.read_only_path.exists()
    assert stat_mode(journal.read_only_path) == 0o600
    with pytest.raises(RuntimeEventJournalError, match="degraded and read-only"):
        RuntimeEventJournal(str(journal_env), "ses_journal").append(
            _event(2, "usage.updated", turn=""))


def test_late_terminal_audit_event_replays_without_state_regression(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    live = TUIEventController(journal=journal)
    live.ingest(_event(1, "turn.started"))
    live.ingest(_event(2, "turn.completed"))

    assert live.ingest(_event(
        3, "message.completed", payload={"item_id": "late", "text": "ignored"})) is None
    assert len(journal.recover().events) == 3
    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_env), "ses_journal"))

    assert rebuilt.snapshot().turn_state == TurnState.SUCCEEDED
    assert rebuilt.snapshot().assistant_text == ""
    assert rebuilt.recovery.skipped_events == 1
    assert not rebuilt.recovery.degraded and not rebuilt.recovery.read_only


def test_message_capacity_is_rejected_before_journal_commit(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    controller = TUIEventController(entity_limit=2, journal=journal)
    events = [
        _event(1, "turn.started"),
        _event(2, "message.completed", payload={"item_id": "m1", "text": "one"}),
        _event(3, "message.completed", payload={"item_id": "m2", "text": "two"}),
    ]
    for event in events:
        controller.ingest(event)
    before = journal.path.stat().st_size

    with pytest.raises(OverflowError, match="message item projection is full"):
        controller.ingest(_event(
            4, "message.completed", payload={"item_id": "m3", "text": "three"}))

    assert journal.path.stat().st_size == before
    assert not controller.read_only_reason
    assert controller.ingest(_event(
        4, "message.completed", event_id="evt_replace_m2",
        payload={"item_id": "m2", "text": "updated"})) is not None


def test_huge_usage_integer_is_skipped_without_poisoning_journal(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    controller = TUIEventController(journal=journal)
    controller.ingest(_event())

    view = controller.ingest(_event(
        2, "usage.updated", payload={"usage": {"huge": 10 ** 4_000}}))

    assert view is not None
    assert "huge" not in controller.projection.stats.usage
    assert not controller.read_only_reason
    assert len(journal.recover().events) == 2


def test_capacity_failure_latches_controller_read_only(journal_env):
    first = _event()
    limit = len(first.to_jsonl().encode("utf-8"))
    journal = RuntimeEventJournal(
        str(journal_env), "ses_journal", max_bytes=limit)
    controller = TUIEventController(journal=journal)
    controller.ingest(first)

    with pytest.raises(RuntimeEventJournalError, match="capacity exhausted"):
        controller.ingest(_event(2, "turn.completed"))
    assert "read-only" in controller.read_only_reason
    with pytest.raises(RuntimeError, match="read-only"):
        controller.ingest(_event(2, "turn.failed"))


def test_recovered_non_terminal_turn_is_presented_as_unknown(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event())

    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_env), "ses_journal"))

    assert rebuilt.projection.turns["turn_one"].state == TurnState.UNKNOWN
    assert rebuilt.snapshot().turn_state == TurnState.UNKNOWN
    assert rebuilt.recovery.unresolved_state == TurnState.RUNNING
    assert "as UNKNOWN" in rebuilt.recovery.warnings[-1]


@pytest.mark.parametrize("events,prior", [
    ([_event(1, "turn.queued")], TurnState.QUEUED),
    ([_event(1, "turn.started")], TurnState.RUNNING),
    ([_event(1, "turn.started"), _event(2, "turn.interrupting")],
     TurnState.INTERRUPTING),
])
def test_each_recovered_nonterminal_state_becomes_unknown(
        journal_env, events, prior):
    session = f"ses_{prior.lower()}"
    journal = RuntimeEventJournal(str(journal_env), session)
    for event in events:
        journal.append(replace(event, session_id=session))

    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_env), session))

    assert rebuilt.snapshot().turn_state == TurnState.UNKNOWN
    assert rebuilt.projection.turns["turn_one"].state == TurnState.UNKNOWN
    assert rebuilt.projection.sessions[session].terminal_state == TurnState.UNKNOWN
    assert rebuilt.recovery.unresolved_state == prior
    assert rebuilt.recovery.read_only


def test_hidden_nonterminal_run_also_makes_recovery_read_only(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    events = [
        _event(1, "turn.started", run="run_a", turn="turn_a"),
        _event(1, "turn.started", run="run_b", turn="turn_b"),
        _event(2, "turn.completed", run="run_b", turn="turn_b"),
    ]
    for event in events:
        journal.append(event)

    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_env), "ses_journal"))

    assert rebuilt.snapshot().turn_state == TurnState.SUCCEEDED
    assert rebuilt.projection.turns["turn_a"].state == TurnState.UNKNOWN
    assert rebuilt.projection.turns["turn_b"].state == TurnState.SUCCEEDED
    assert rebuilt.projection.sessions["ses_journal"].terminal_state == TurnState.UNKNOWN
    assert rebuilt.recovery.unresolved_state == TurnState.RUNNING
    assert rebuilt.recovery.read_only


@pytest.mark.parametrize("tail,state", [
    ([_event(2, "turn.completed")], TurnState.SUCCEEDED),
    ([_event(2, "turn.failed")], TurnState.FAILED),
    ([_event(2, "turn.interrupting"), _event(3, "turn.interrupted")],
     TurnState.INTERRUPTED),
])
def test_recovered_confirmed_terminal_states_remain_writable(
        journal_env, tail, state):
    session = f"ses_{state.lower()}"
    journal = RuntimeEventJournal(str(journal_env), session)
    journal.append(replace(_event(), session_id=session))
    for event in tail:
        journal.append(replace(event, session_id=session))

    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_env), session))

    assert rebuilt.snapshot().turn_state == state
    assert rebuilt.recovery.unresolved_state == ""
    assert not rebuilt.recovery.read_only


def test_controller_blocks_live_events_after_degraded_recovery(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event())
    with journal.path.open("ab") as handle:
        handle.write(b"{}\n")
    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_env), "ses_journal"))

    assert rebuilt.recovery.degraded
    with pytest.raises(RuntimeError, match="degraded and read-only"):
        rebuilt.ingest(_event(2, "message.delta", payload={"text": "later"}))


class _ProjectionHarness(TUIProjectionMixin):
    def __init__(self, cwd):
        self._backend = type("Backend", (), {
            "cwd": str(cwd), "session_id": "ses_journal"})()
        self._adapter_session_id = ""
        self._init_runtime_projection()


def test_projection_mixin_rotates_journal_when_runtime_session_changes(journal_env):
    harness = _ProjectionHarness(journal_env)
    harness._enable_runtime_projection()
    first = harness._event_controller
    first.ingest(_event())

    harness._backend.session_id = "ses_second"
    harness._prepare_runtime_projection()
    second = harness._event_controller
    second.ingest(_event(session="ses_second"))

    assert first is not second
    with pytest.raises(RuntimeError, match="closed"):
        first.ingest(_event(2, "message.delta", payload={"text": "late"}))
    assert len(RuntimeEventJournal(
        str(journal_env), "ses_journal").recover().events) == 1
    assert len(RuntimeEventJournal(
        str(journal_env), "ses_second").recover().events) == 1


def test_projection_mixin_refuses_unresolved_recovered_session(journal_env):
    journal = RuntimeEventJournal(str(journal_env), "ses_journal")
    assert journal.append(_event())
    harness = _ProjectionHarness(journal_env)

    with pytest.raises(RuntimeError, match="journal recovery is read-only"):
        harness._enable_runtime_projection()


def test_projection_mixin_can_disable_persistent_journal(
        journal_env, monkeypatch):
    monkeypatch.setattr(config, "TUI_JOURNAL_ENABLED", False)
    harness = _ProjectionHarness(journal_env)

    harness._enable_runtime_projection()
    harness._event_controller.ingest(_event())

    assert harness._journal_identity == ()
    assert not (journal_env / "runtime-data" / "tui").exists()


def test_same_session_cannot_cross_scope_boundary(journal_env, monkeypatch):
    harness = _ProjectionHarness(journal_env)
    harness._enable_runtime_projection()
    original = harness._event_controller
    monkeypatch.setenv("PBATCH_MEMORY_TENANT", "other-tenant")

    with pytest.raises(RuntimeError, match="cannot cross"):
        harness._prepare_runtime_projection()
    assert harness._event_controller is original


def stat_mode(path):
    return os.stat(path, follow_symlinks=False).st_mode & 0o777
