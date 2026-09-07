"""Bounded local session summary cache contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading

import pytest

from pbatch.tui_controller import TUIEventController
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_protocol import RuntimeEvent
from pbatch.tui_session_summary import read_session_summary
from pbatch.tui_sessions import discover_tui_sessions, recover_resume_target


def _event(
        session: str, seq: int, kind: str, *, payload=None,
        runtime: str = "codex-app-server", authoritative: bool = False
        ) -> RuntimeEvent:
    extensions = {"runtime": runtime}
    if authoritative:
        extensions["authoritative"] = True
    return RuntimeEvent(
        event_id=f"evt_{session}_{seq}_{kind.replace('.', '_')}",
        seq=seq,
        occurred_at=f"2026-08-26T12:00:{seq:02d}Z",
        workspace_id="ws_summary",
        session_id=session,
        run_id="run_summary",
        turn_id="" if kind.startswith("session.") else "turn_summary",
        actor_id="agent_main",
        kind=kind,
        payload=payload or {},
        extensions=extensions,
    )


@pytest.fixture
def summary_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime-data"))
    return cwd


def _completed(cwd: Path, session: str = "ses_summary") -> RuntimeEventJournal:
    journal = RuntimeEventJournal(str(cwd), session)
    controller = TUIEventController(journal=journal)
    events = [
        _event(session, 1, "session.created", payload={
            "provider_session_id": "thread-summary"}),
        _event(session, 2, "turn.started"),
        _event(session, 3, "workspace.snapshot", payload={
            "workspace": {"branch": "feature/summary"}}, authoritative=True),
        _event(session, 4, "message.completed", payload={
            "item_id": "message-one", "text": "\x1b[31mAnswer\x1b[0m\nready"}),
        _event(session, 5, "turn.completed"),
    ]
    for event in events:
        controller.ingest(event)
    controller.close()
    return journal


def test_projection_commit_writes_bounded_redacted_summary(summary_cwd):
    journal = _completed(summary_cwd)

    summary, status = read_session_summary(journal, journal.path.stat().st_size)

    assert status == "" and summary is not None
    assert summary.provider_session_id == "thread-summary"
    assert summary.runtime == "codex-app-server"
    assert summary.workspace_id == "ws_summary"
    assert summary.branch == "feature/summary"
    assert summary.turn_state == "SUCCEEDED"
    assert summary.requires_reconciliation is False
    assert summary.read_only is False
    assert summary.preview == "Answer ready"
    assert summary.last_event_id.endswith("turn_completed")
    cached = json.loads(journal.summary_path.read_text(encoding="utf-8"))
    metadata = json.loads(journal.meta_path.read_text(encoding="utf-8"))
    assert cached["cwd_id"] == metadata["cwd_id"]


def test_live_unresolved_summary_is_fail_closed_but_only_advisory(summary_cwd):
    session = "ses_running"
    journal = RuntimeEventJournal(str(summary_cwd), session)
    controller = TUIEventController(journal=journal)
    controller.ingest(_event(session, 1, "session.created", payload={
        "provider_session_id": "thread-running"}))
    controller.ingest(_event(session, 2, "turn.started"))
    controller.close()

    record = next(item for item in discover_tui_sessions(
        str(summary_cwd)).sessions if item.session_id == session)

    assert record.summary is not None
    assert record.summary.turn_state == "RUNNING"
    assert record.summary.requires_reconciliation is True
    assert record.read_only is True
    assert recover_resume_target(journal.recover()).provider_session_id == "thread-running"


def test_recovered_unknown_turn_remains_marked_for_reconciliation(summary_cwd):
    session = "ses_recovered_running"
    journal = RuntimeEventJournal(str(summary_cwd), session)
    journal.append(_event(session, 1, "session.created", payload={
        "provider_session_id": "thread-recovered"}))
    journal.append(_event(session, 2, "turn.started"))

    controller = TUIEventController(journal=RuntimeEventJournal(
        str(summary_cwd), session))
    controller.close()
    summary, status = read_session_summary(journal, journal.path.stat().st_size)

    assert status == "" and summary is not None
    assert summary.turn_state == "UNKNOWN"
    assert summary.requires_reconciliation is True
    assert summary.read_only is True


def test_summary_uses_the_persisted_redacted_event_not_raw_provider_text(summary_cwd):
    session = "ses_secret"
    journal = RuntimeEventJournal(str(summary_cwd), session)
    controller = TUIEventController(journal=journal)
    controller.ingest(_event(session, 1, "session.created", payload={
        "provider_session_id": "api_key=provider-secret"}))
    controller.close()

    summary, status = read_session_summary(journal, journal.path.stat().st_size)

    assert status == "" and summary is not None
    assert summary.provider_session_id == ""
    assert b"provider-secret" not in journal.path.read_bytes()
    assert b"provider-secret" not in journal.summary_path.read_bytes()


def test_stale_or_invalid_sidecar_never_hides_local_session(summary_cwd):
    journal = _completed(summary_cwd, "ses_stale")
    original = journal.summary_path.read_bytes()
    journal.append(_event("ses_stale", 6, "runtime.warning", payload={
        "message": "later"}))

    summary, status = read_session_summary(journal, journal.path.stat().st_size)
    assert summary is None and status == "stale"
    record = next(item for item in discover_tui_sessions(
        str(summary_cwd)).sessions if item.session_id == "ses_stale")
    assert record.summary is None and record.summary_status == "stale"
    assert journal.summary_path.read_bytes() == original
    assert recover_resume_target(journal.recover()).provider_session_id == "thread-summary"

    journal.summary_path.write_text("not-json", encoding="utf-8")
    record = next(item for item in discover_tui_sessions(
        str(summary_cwd)).sessions if item.session_id == "ses_stale")
    assert record.summary is None and record.summary_status == "invalid"


def test_summary_tail_identity_is_checked_at_the_locked_boundary(summary_cwd):
    journal = _completed(summary_cwd, "ses_tail_identity")
    value = json.loads(journal.summary_path.read_text(encoding="utf-8"))
    value["last_event_id"] = "x" * len(value["last_event_id"])
    journal.summary_path.write_text(json.dumps(value), encoding="utf-8")

    summary, status = read_session_summary(journal, journal.path.stat().st_size)

    assert summary is None and status == "stale"


def test_deeply_nested_invalid_sidecar_degrades_without_worker_failure(summary_cwd):
    journal = _completed(summary_cwd, "ses_recursive_sidecar")
    nested = '{"x":' + "[" * 1_500 + "0" + "]" * 1_500 + "}"
    journal.summary_path.write_text(nested, encoding="utf-8")

    summary, status = read_session_summary(journal, journal.path.stat().st_size)

    assert summary is None and status == "invalid"


def test_old_journal_is_lazily_backfilled_only_when_explicitly_opened(summary_cwd):
    session = "ses_legacy_cache"
    journal = RuntimeEventJournal(str(summary_cwd), session)
    journal.append(_event(session, 1, "session.created", payload={
        "provider_session_id": "thread-legacy"}))
    journal.append(_event(session, 2, "turn.started"))
    journal.append(_event(session, 3, "turn.completed"))
    assert not journal.summary_path.exists()
    before = next(item for item in discover_tui_sessions(
        str(summary_cwd)).sessions if item.session_id == session)
    assert before.summary_status == "missing"

    controller = TUIEventController(journal=journal)
    controller.close()

    after = next(item for item in discover_tui_sessions(
        str(summary_cwd)).sessions if item.session_id == session)
    assert after.summary is not None
    assert after.summary.provider_session_id == "thread-legacy"
    assert after.summary.turn_state == "SUCCEEDED"


def test_multi_writer_gap_never_publishes_an_exact_but_incomplete_summary(
        summary_cwd):
    session = "ses_multi_writer"
    journal_a = RuntimeEventJournal(str(summary_cwd), session)
    controller_a = TUIEventController(journal=journal_a)
    controller_a.ingest(_event(session, 1, "session.created", payload={
        "provider_session_id": "thread-one"}))
    journal_b = RuntimeEventJournal(str(summary_cwd), session)
    controller_b = TUIEventController(journal=journal_b)
    controller_b.ingest(_event(session, 2, "session.resumed", payload={
        "provider_session_id": "thread-two"}))

    warning = _event(session, 3, "runtime.warning", payload={
        "message": "writer A continued"})
    warning.turn_id = ""
    controller_a.ingest(warning)
    controller_a.close()
    controller_b.close()

    summary, status = read_session_summary(
        journal_a, journal_a.path.stat().st_size)
    assert summary is None and status == "stale"
    target = recover_resume_target(journal_a.recover())
    assert target is not None and target.provider_session_id == "thread-two"


@pytest.mark.parametrize("late_kind", ["semantic-error", "late-message"])
def test_recovery_never_summarizes_projection_rejected_suffix(
        summary_cwd, late_kind):
    session = f"ses_rejected_{late_kind}"
    journal = RuntimeEventJournal(str(summary_cwd), session)
    journal.append(_event(session, 1, "session.created", payload={
        "provider_session_id": "thread-original"}))
    if late_kind == "semantic-error":
        journal.append(_event(session, 2, "turn.completed"))
        journal.append(_event(session, 3, "session.resumed", payload={
            "provider_session_id": "thread-rejected"}))
    else:
        journal.append(_event(session, 2, "turn.started"))
        journal.append(_event(session, 3, "turn.completed"))
        journal.append(_event(session, 4, "message.completed", payload={
            "item_id": "late-message", "text": "late rejected preview"}))

    controller = TUIEventController(journal=RuntimeEventJournal(
        str(summary_cwd), session))
    report = controller.recovery
    controller.close()

    summary, status = read_session_summary(journal, journal.path.stat().st_size)
    assert summary is None and status == "missing"
    if late_kind == "semantic-error":
        assert report.degraded is True
    else:
        assert report.skipped_events == 1


def test_latest_session_event_atomically_replaces_provider_runtime_pair(
        summary_cwd):
    session = "ses_handle_pair"
    journal = RuntimeEventJournal(str(summary_cwd), session)
    controller = TUIEventController(journal=journal)
    created = _event(session, 1, "session.created", payload={
        "provider_session_id": "thread-original"})
    created.extensions["model"] = "trusted-model"
    controller.ingest(created)
    controller.ingest(_event(session, 2, "session.resumed", payload={}))
    controller.close()

    summary, status = read_session_summary(journal, journal.path.stat().st_size)
    assert status == "" and summary is not None
    assert summary.provider_session_id == ""
    assert summary.runtime == ""
    assert summary.model == ""
    assert recover_resume_target(journal.recover()) is None


def test_non_lifecycle_content_cannot_replace_runtime_or_model(summary_cwd):
    session = "ses_model_provenance"
    journal = RuntimeEventJournal(str(summary_cwd), session)
    controller = TUIEventController(journal=journal)
    created = _event(session, 1, "session.created", payload={
        "provider_session_id": "thread-model"})
    created.extensions["model"] = "trusted-model"
    controller.ingest(created)
    controller.ingest(_event(session, 2, "turn.started"))
    forged = _event(session, 3, "message.completed", payload={
        "item_id": "message-model", "text": "answer", "model": "forged-model"},
        runtime="forged-runtime")
    forged.extensions["model"] = "forged-extension-model"
    controller.ingest(forged)
    controller.ingest(_event(session, 4, "turn.completed"))
    controller.close()

    summary, status = read_session_summary(journal, journal.path.stat().st_size)
    assert status == "" and summary is not None
    assert summary.runtime == "codex-app-server"
    assert summary.model == "trusted-model"


def test_discovery_reads_summaries_only_after_top_limit(summary_cwd, monkeypatch):
    old = _completed(summary_cwd, "ses_old")
    recent = _completed(summary_cwd, "ses_recent")
    os.utime(old.path, ns=(1, 1))
    os.utime(recent.path, ns=(2, 2))
    from pbatch import tui_sessions

    calls = []
    original = tui_sessions.read_session_summary

    def observed(journal, journal_bytes):
        calls.append(journal.session_id)
        return original(journal, journal_bytes)

    monkeypatch.setattr(tui_sessions, "read_session_summary", observed)
    monkeypatch.setattr(
        RuntimeEventJournal, "recover",
        lambda _self: (_ for _ in ()).throw(AssertionError("index replayed journal")))

    index = discover_tui_sessions(str(summary_cwd), limit=1)

    assert [item.session_id for item in index.sessions] == ["ses_recent"]
    assert calls == ["ses_recent"]


def test_summary_symlink_is_ignored_and_provider_handle_stays_journal_owned(
        summary_cwd):
    journal = _completed(summary_cwd, "ses_boundary")
    outside = summary_cwd.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    journal.summary_path.unlink()
    journal.summary_path.symlink_to(outside)

    record = next(item for item in discover_tui_sessions(
        str(summary_cwd)).sessions if item.session_id == "ses_boundary")

    assert record.summary is None and record.summary_status == "invalid"
    assert recover_resume_target(journal.recover()).provider_session_id == "thread-summary"


def test_summary_fifo_is_rejected_without_blocking_session_discovery(summary_cwd):
    journal = _completed(summary_cwd, "ses_fifo")
    journal.summary_path.unlink()
    os.mkfifo(journal.summary_path)
    result = []

    worker = threading.Thread(
        target=lambda: result.append(read_session_summary(
            journal, journal.path.stat().st_size)),
        daemon=True)
    worker.start()
    worker.join(timeout=1.0)
    blocked = worker.is_alive()
    if blocked:
        descriptor = os.open(journal.summary_path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(descriptor)
        worker.join(timeout=1.0)

    assert blocked is False
    assert result == [(None, "invalid")]
