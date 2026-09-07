"""Safety regressions for durable, replay-equivalent TUI journals."""

from __future__ import annotations

import json
import math

import pytest

from pbatch.tui_controller import TUIEventController
import pbatch.tui_journal as tui_journal
from pbatch.tui_journal import RuntimeEventJournal, RuntimeEventJournalError
from pbatch.tui_protocol import RuntimeEvent, TurnState


@pytest.fixture
def journal_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime-data"))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    return cwd


def _event(seq=1, kind="turn.started", *, event_id=None, turn_id="turn_one",
           payload=None, extensions=None):
    return RuntimeEvent(
        event_id=event_id or f"evt_{seq}_{kind.replace('.', '_')}",
        seq=seq,
        occurred_at=f"2026-08-23T12:00:{seq:02d}Z",
        workspace_id="ws_one",
        session_id="ses_safety",
        run_id="run_one",
        turn_id=turn_id,
        actor_id="agent_main",
        kind=kind,
        payload=payload or {},
        extensions=extensions or {},
    )


def test_ambiguous_append_is_not_committed_by_a_new_journal_instance(
        journal_cwd, monkeypatch):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    started = _event()
    ambiguous = _event(2, "turn.completed")
    assert journal.append(started)

    with monkeypatch.context() as patch:
        patch.setattr(
            tui_journal, "_fsync_file",
            lambda _descriptor: (_ for _item in ()).throw(
                OSError("simulated uncertain fsync")))
        patch.setattr(tui_journal, "_rollback_append", lambda *_args: False)
        with pytest.raises(RuntimeEventJournalError) as failure:
            journal.append(ambiguous)

    assert failure.value.retryable is False
    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_cwd), "ses_safety"))
    recovered_ids = {
        event.event_id for event in rebuilt.recovery.journal.events
    }
    assert ambiguous.event_id not in recovered_ids
    assert rebuilt.snapshot().turn_state != TurnState.SUCCEEDED


def test_commit_only_marker_recovers_finalized_event_before_cleanup(
        journal_cwd, monkeypatch):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    event = _event()
    original_clear = tui_journal._clear_json

    def crash_before_commit_cleanup(path):
        if path == journal.commit_path:
            raise SystemExit("simulated process crash")
        return original_clear(path)

    with monkeypatch.context() as patch:
        patch.setattr(tui_journal, "_clear_json", crash_before_commit_cleanup)
        with pytest.raises(SystemExit):
            journal.append(event)

    assert journal.commit_path.is_file()
    recovered = RuntimeEventJournal(
        str(journal_cwd), "ses_safety").recover()
    assert [item.event_id for item in recovered.events] == [event.event_id]
    assert not recovered.degraded
    assert "confirmed" in " ".join(recovered.warnings)


def test_intent_cleanup_failure_rolls_back_before_delivery(journal_cwd, monkeypatch):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    event = _event()
    original_clear = tui_journal._clear_json

    def fail_intent_clear(path):
        if path == journal.intent_path:
            raise RuntimeEventJournalError("simulated intent clear failure")
        return original_clear(path)

    with monkeypatch.context() as patch:
        patch.setattr(tui_journal, "_clear_json", fail_intent_clear)
        with pytest.raises(RuntimeEventJournalError) as failure:
            journal.append(event)

    recovered = RuntimeEventJournal(
        str(journal_cwd), "ses_safety").recover()
    assert failure.value.retryable
    assert not recovered.events
    assert recovered.writable


def test_intent_unlink_fsync_failure_does_not_report_committed_event_failed(
        journal_cwd, monkeypatch):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    event = _event()
    original_clear = tui_journal._clear_json

    def unlink_then_fail(path):
        if path == journal.intent_path:
            path.unlink()
            raise RuntimeEventJournalError("simulated directory fsync failure")
        return original_clear(path)

    with monkeypatch.context() as patch:
        patch.setattr(tui_journal, "_clear_json", unlink_then_fail)
        assert journal.append(event)

    assert not journal.intent_path.exists()
    assert not journal.commit_path.exists()
    recovered = RuntimeEventJournal(
        str(journal_cwd), "ses_safety").recover()
    assert [item.event_id for item in recovered.events] == [event.event_id]


def test_commit_unlink_fsync_failure_still_returns_committed_success(
        journal_cwd, monkeypatch):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    event = _event()
    original_clear = tui_journal._clear_json

    def unlink_then_fail(path):
        if path == journal.commit_path:
            path.unlink()
            raise RuntimeEventJournalError("simulated directory fsync failure")
        return original_clear(path)

    with monkeypatch.context() as patch:
        patch.setattr(tui_journal, "_clear_json", unlink_then_fail)
        assert journal.append(event)

    assert not journal.intent_path.exists()
    assert not journal.commit_path.exists()
    recovered = RuntimeEventJournal(
        str(journal_cwd), "ses_safety").recover()
    assert [item.event_id for item in recovered.events] == [event.event_id]
    assert not recovered.degraded


def test_commit_directory_fsync_failure_never_replays_undelivered_event(
        journal_cwd, monkeypatch):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    event = _event()
    original_fsync_directory = tui_journal._fsync_directory

    def fail_after_commit_replace(path):
        if journal.commit_path.exists():
            raise RuntimeEventJournalError("simulated commit directory fsync error")
        return original_fsync_directory(path)

    with monkeypatch.context() as patch:
        patch.setattr(tui_journal, "_fsync_directory", fail_after_commit_replace)
        patch.setattr(tui_journal, "_rollback_append", lambda *_args: False)
        with pytest.raises(RuntimeEventJournalError) as failure:
            journal.append(event)

    assert failure.value.journal_fatal
    assert journal.intent_path.exists()
    assert journal.commit_path.exists()
    if journal.read_only_path.exists():
        journal.read_only_path.unlink()  # simulate this later marker being lost
    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_cwd), "ses_safety"))
    assert event.event_id not in {
        item.event_id for item in rebuilt.recovery.journal.events}
    assert not rebuilt.recovery.read_only
    assert rebuilt.snapshot().turn_state != TurnState.SUCCEEDED


def test_workspace_snapshot_compaction_preserves_rebuild_projection(journal_cwd):
    noise = [{"id": str(index)} for index in range(1_500)]
    event = _event(
        kind="workspace.snapshot",
        turn_id="",
        payload={
            "phase": "after",
            "workspace": {
                "available": True,
                "root": "/repo",
                "branch": "main",
                "head": "abc123",
            },
            "delta": {
                "changed": True,
                "dirty_files": 1,
                "paths": ["new.py"],
                "head_changed": True,
            },
            "noise": noise,
        },
        extensions={"runtime": "test", "authoritative": True},
    )
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    live = TUIEventController(journal=journal)

    live_view = live.ingest(event)
    assert live_view is not None
    persisted = journal.recover().events[0]
    assert persisted.payload.get("journal_payload_truncated") is True

    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_cwd), "ses_safety"))
    assert rebuilt.snapshot().workspace == live_view.workspace


@pytest.mark.parametrize(("kind", "payload", "turn_scoped"), [
    ("message.completed", {"item_id": "answer", "text": "done"}, True),
    ("turn.failed", {"state": "FAILED", "reason": "boom"}, True),
    ("runtime.warning", {"stream": "stderr", "text": "careful"}, False),
    ("command.completed", {
        "item_id": "command-1", "command": "check", "exit_code": 0,
        "status": "completed"}, True),
    ("command.output", {
        "item_id": "command-1", "aggregated_output": "output"}, True),
    ("file.changed", {"paths": ["one.py", "two.py"]}, True),
    ("plan.updated", {"item_id": "plan-1", "items": [
        {"text": "first", "status": "completed"},
        {"text": "second", "status": "in_progress"}]}, True),
    ("usage.updated", {"usage": {
        "input_tokens": 7, "output_tokens": 3}}, False),
])
def test_compaction_preserves_each_projected_event_family(
        journal_cwd, kind, payload, turn_scoped):
    noise = [{"id": str(index)} for index in range(1_500)]
    event_payload = dict(payload, noise=noise)
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    live = TUIEventController(journal=journal)
    seq = 1
    if turn_scoped:
        live.ingest(_event())
        seq = 2
    target = _event(
        seq, kind, event_id=f"evt_compact_{kind.replace('.', '_')}",
        turn_id="turn_one" if turn_scoped else "", payload=event_payload)

    projected = live.ingest(target)
    if turn_scoped and kind != "turn.failed":
        live.ingest(_event(
            seq + 1, "turn.completed", event_id=f"evt_finish_{kind.replace('.', '_')}"))
    live_view = live.snapshot()
    persisted = next(item for item in journal.recover().events
                     if item.event_id == target.event_id)
    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_cwd), "ses_safety"))

    assert projected is not None
    assert persisted.payload.get("journal_payload_truncated") is True
    assert rebuilt.snapshot() == live_view
    assert rebuilt.projection.stats.usage == live.projection.stats.usage


def test_near_limit_projection_payload_relocates_compaction_metadata(journal_cwd):
    event = _event(
        kind="runtime.warning", turn_id="",
        payload={
            "text": "x" * 65_401,
            "noise": [{"id": str(index)} for index in range(3)],
        },
    )
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    live = TUIEventController(journal=journal)

    live_view = live.ingest(event)
    persisted = journal.recover().events[0]
    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_cwd), "ses_safety"))

    assert live_view is not None
    assert persisted.payload["text"] == event.payload["text"]
    assert persisted.extensions.get("journal_payload_truncated") is True
    assert rebuilt.snapshot() == live_view


def test_near_limit_message_relocates_identity_without_replay_drift(journal_cwd):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    live = TUIEventController(journal=journal)
    live.ingest(_event())
    target = _event(
        2, "message.completed", event_id="evt_near_limit_message",
        payload={"item_id": "1", "text": "x" * 65_467})

    assert live.ingest(target) is not None
    live.ingest(_event(3, "turn.completed", event_id="evt_near_limit_finish"))
    persisted = next(event for event in journal.recover().events
                     if event.event_id == target.event_id)
    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_cwd), "ses_safety"))

    identities = persisted.extensions.get("journal_payload_identities", {})
    assert "item_id" not in persisted.payload
    assert str(identities.get("item_id", "")).startswith("id:")
    assert rebuilt.snapshot() == live.snapshot()


def test_identity_digest_matches_live_string_normalization(journal_cwd):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    live = TUIEventController(journal=journal)
    events = [
        _event(),
        _event(2, "message.completed", event_id="evt_numeric_item",
               payload={"item_id": 1, "text": "first"}),
        _event(3, "message.completed", event_id="evt_string_item",
               payload={"item_id": "1", "text": "second"}),
        _event(4, "turn.completed", event_id="evt_identity_finish"),
    ]
    for event in events:
        live.ingest(event)

    persisted = journal.recover().events
    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_cwd), "ses_safety"))

    identities = [event.payload["item_id"] for event in persisted
                  if event.kind == "message.completed"]
    assert identities[0] == identities[1]
    assert live.snapshot().assistant_text == "second"
    assert rebuilt.snapshot() == live.snapshot()


def test_falsey_identity_matches_live_implicit_item_semantics(journal_cwd):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    live = TUIEventController(journal=journal)
    events = [
        _event(),
        _event(2, "message.completed", event_id="evt_falsey_item",
               payload={"item_id": 0, "text": "first"}),
        _event(3, "message.completed", event_id="evt_explicit_zero",
               payload={"item_id": "0", "text": "second"}),
        _event(4, "turn.completed", event_id="evt_falsey_finish"),
    ]
    for event in events:
        live.ingest(event)

    persisted = journal.recover().events
    rebuilt = TUIEventController(journal=RuntimeEventJournal(
        str(journal_cwd), "ses_safety"))
    identities = [event.payload["item_id"] for event in persisted
                  if event.kind == "message.completed"]

    assert identities[0] == ""
    assert identities[1].startswith("id:")
    assert live.snapshot().assistant_text == "first\n\nsecond"
    assert rebuilt.snapshot() == live.snapshot()


def test_non_scalar_projection_identity_is_rejected_before_journaling(journal_cwd):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    controller = TUIEventController(journal=journal)
    controller.ingest(_event())
    before = journal.path.stat().st_size

    with pytest.raises(ValueError, match="item_id identity must be a scalar"):
        controller.ingest(_event(
            2, "message.completed", event_id="evt_mapping_item",
            payload={"item_id": {"a": 1}, "text": "unsafe identity"}))

    assert journal.path.stat().st_size == before
    assert not controller.read_only_reason


def test_nested_non_scalar_identity_does_not_poison_controller(journal_cwd):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    controller = TUIEventController(journal=journal)
    controller.ingest(_event())
    before = journal.path.stat().st_size

    with pytest.raises(ValueError, match="request_id identity must be a scalar"):
        controller.ingest(_event(
            2, "approval.requested", event_id="evt_mapping_request",
            payload={"requests": [{"request_id": {"nested": 1}}]}))

    assert journal.path.stat().st_size == before
    assert not controller.read_only_reason
    view = controller.ingest(_event(
        2, "turn.completed", event_id="evt_after_bad_identity"))
    assert view is not None and view.turn_state == TurnState.SUCCEEDED


def test_unknown_field_identity_is_rejected_without_poisoning(journal_cwd):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    controller = TUIEventController(journal=journal)
    controller.ingest(_event())
    before = journal.path.stat().st_size
    mapping = json.loads(_event(
        2, "approval.requested", event_id="evt_future_identity").to_jsonl())
    mapping["future_contract"] = {"request_id": {"nested": 1}}
    invalid = RuntimeEvent.from_jsonl(json.dumps(mapping))

    with pytest.raises(ValueError, match="request_id identity must be a scalar"):
        controller.ingest(invalid)

    assert journal.path.stat().st_size == before
    assert not controller.read_only_reason
    assert controller.ingest(_event(
        2, "turn.completed", event_id="evt_after_future_identity")) is not None


def test_cyclic_payload_is_canonicalized_before_identity_walk(journal_cwd):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    controller = TUIEventController(journal=journal)
    controller.ingest(_event())
    before = journal.path.stat().st_size
    invalid = _event(2, "runtime.warning", turn_id="")
    invalid.payload["loop"] = invalid.payload

    with pytest.raises(ValueError, match="payload must be valid JSON"):
        controller.ingest(invalid)

    assert journal.path.stat().st_size == before
    assert not controller.read_only_reason
    assert controller.ingest(_event(
        2, "turn.completed", event_id="evt_after_cyclic_payload")) is not None


def test_usage_accumulation_never_creates_non_finite_values(journal_cwd):
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    controller = TUIEventController(journal=journal)
    for seq in (1, 2):
        controller.ingest(_event(
            seq,
            "usage.updated",
            turn_id="",
            payload={"usage": {"tokens": 1e308}},
        ))

    global_usage = controller.projection.stats.usage
    run_usage = controller.projection.stats_by_run["run_one"].usage
    assert all(math.isfinite(value) for value in global_usage.values())
    assert all(math.isfinite(value) for value in run_usage.values())


def test_secret_like_event_id_is_never_persisted(journal_cwd):
    secret = "sk-proj-ABCDEFGHIJKL"
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    event = _event(
        kind="runtime.warning",
        event_id=f"evt_x-{secret}",
        turn_id="",
        payload={"text": "safe"},
    )

    before = journal.path.stat().st_size if journal.path.exists() else 0
    with pytest.raises(RuntimeEventJournalError, match="secret"):
        journal.append(event)

    raw = journal.path.read_bytes() if journal.path.exists() else b""
    assert len(raw) == before
    assert secret.encode("utf-8") not in raw


def test_secret_like_recursive_mapping_key_is_never_persisted(journal_cwd):
    secret = "sk-proj-ABCDEFGHIJKL"
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    event = _event(
        kind="runtime.warning",
        turn_id="",
        payload={
            "outer": [
                {"deeper": {f"note-{secret}": "safe"}},
            ],
        },
    )

    before = journal.path.stat().st_size if journal.path.exists() else 0
    with pytest.raises(RuntimeEventJournalError, match="secret"):
        journal.append(event)

    raw = journal.path.read_bytes() if journal.path.exists() else b""
    assert len(raw) == before
    assert secret.encode("utf-8") not in raw


def test_non_string_envelope_scalar_is_rejected_before_persistence(journal_cwd):
    with pytest.raises(ValueError, match="workspace_id must be a string"):
        RuntimeEvent(
            event_id="evt_bad_envelope", seq=1,
            occurred_at="2026-08-23T12:00:01Z",
            workspace_id={"api_key": "sk-proj-ABCDEFGHIJKL"},
            session_id="ses_safety", run_id="run_one", turn_id="",
            actor_id="agent_main", kind="runtime.warning", payload={},
        )
    data_root = journal_cwd.parent / "runtime-data"
    assert not (data_root / "tui").exists()


def test_numeric_request_ids_use_stable_fixed_width_digests(journal_cwd):
    repeated = 123_456_789_012_345_678
    distinct = repeated + 1
    journal = RuntimeEventJournal(str(journal_cwd), "ses_safety")
    event = _event(
        kind="approval.requested",
        payload={
            "requests": [
                {"request_id": 0},
                {"request_id": repeated},
                {"request_id": repeated},
                {"request_id": distinct},
                {"request_id": "reference-request"},
            ],
        },
    )

    assert journal.append(event)
    requests = journal.recover().events[0].payload["requests"]
    identities = [item["request_id"] for item in requests]

    assert all(isinstance(identity, str) for identity in identities)
    assert all(identity.startswith("id:") for identity in identities)
    assert len({len(identity.encode("utf-8")) for identity in identities}) == 1
    assert identities[1] == identities[2]
    assert identities[1] != identities[3]
    assert identities[0] != ""
    assert str(repeated).encode("ascii") not in journal.path.read_bytes()
