"""Regression contracts for fatal journals and Legacy session rotation."""

from __future__ import annotations

import threading
import uuid

import pytest

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_adapter_legacy import LegacyAdapter
from pbatch.tui_controller import TUIEventController
from pbatch.tui_journal import RuntimeEventJournal, RuntimeEventJournalError
from pbatch.tui_projection_ui import TUIProjectionMixin
from pbatch.tui_protocol import RuntimeEvent
from pbatch.tui_transcript import TranscriptEntry


@pytest.fixture
def journal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime-data"))
    return tmp_path


def _event(seq: int, kind: str, *, event_id: str = "", session: str = "ses_old",
           run: str = "run_old", turn: str = "turn_old") -> RuntimeEvent:
    return RuntimeEvent(
        event_id=event_id or f"evt_{run}_{seq}_{kind.replace('.', '_')}",
        seq=seq, occurred_at=f"2026-08-23T12:00:{seq:02d}Z",
        workspace_id="ws_rotation", session_id=session, run_id=run,
        turn_id=turn, actor_id="agent_main", kind=kind, payload={},
    )


def test_capacity_fatal_persists_read_only_across_controller_instances(journal_env):
    started = _event(1, "turn.started")
    completed = _event(2, "turn.completed")
    maximum = sum(len(event.to_jsonl().encode("utf-8"))
                  for event in (started, completed))
    journal = RuntimeEventJournal(
        str(journal_env), "ses_old", max_bytes=maximum)
    controller = TUIEventController(journal=journal)
    controller.ingest(started)
    controller.ingest(completed)

    with pytest.raises(RuntimeEventJournalError, match="capacity exhausted"):
        controller.ingest(_event(3, "workspace.snapshot"))

    assert controller.read_only_reason
    assert journal.read_only_path.is_file()
    reopened = TUIEventController(journal=RuntimeEventJournal(
        str(journal_env), "ses_old", max_bytes=maximum))
    assert reopened.recovery.degraded
    assert reopened.recovery.read_only
    with pytest.raises(RuntimeError, match="read-only"):
        reopened.ingest(_event(
            1, "turn.started", event_id="evt_new_run",
            run="run_after_restart", turn="turn_after_restart"))


class _LegacyBackend:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.session_id = "provider-old"
        self.new_session_calls: list[str] = []
        self.emit = lambda _kind, _text: None

    def set_emit(self, emit) -> None:
        self.emit = emit

    def new_session(self, name: str = "") -> dict:
        self.new_session_calls.append(name)
        self.session_id = "provider-new"
        return {"session_id": self.session_id, "session_name": name or "new"}

    def run_turn(self, _prompt: str) -> dict:
        return {"success": True}

    def cancel(self) -> bool:
        return True

    def workspace_status(self) -> dict:
        return {"available": False, "root": self.cwd}

    def workspace_diff(self) -> dict:
        return {}


class _ProjectionHarness(TUIProjectionMixin):
    def __init__(self, backend: _LegacyBackend, adapter: LegacyAdapter,
                 session_id: str) -> None:
        self._backend = backend
        self._adapter = adapter
        self._adapter_session_id = session_id
        self._runtime_blocked_reason = ""
        self._ui_thread_id = threading.get_ident()
        self._init_runtime_projection()

    def _apply_runtime_snapshot(self, view) -> None:
        self._runtime_view = view


class _SessionSwitchApp(PiBatchTUI):
    def _apply_runtime_snapshot(self, view) -> None:
        self._runtime_view = view

    def _append_entry(self, role: str, text: str, *, render: bool = True) -> None:
        self._entries.append(TranscriptEntry(role, text))


def test_full_legacy_queue_rejects_switch_before_backend_or_adapter_mutation(
        journal_env):
    backend = _LegacyBackend(str(journal_env))
    adapter = LegacyAdapter(backend, maxsize=1)
    try:
        old_session = adapter._adopt_session({
            "session_id": "ses_old", "workspace_id": "ws_rotation"})

        with pytest.raises(OverflowError, match="queue is full"):
            adapter.start_session({
                "session_id": "ses_new", "workspace_id": "ws_rotation",
                "name": "must-not-start"})

        assert backend.session_id == "provider-old"
        assert backend.new_session_calls == []
        assert adapter._session is old_session
        queued = adapter.events().get(timeout=0)
        assert queued is not None
        assert queued.kind == "session.created"
        assert queued.session_id == old_session.session_id
    finally:
        adapter.close()


def test_read_only_target_preflight_keeps_current_legacy_session(
        journal_env, monkeypatch):
    backend = _LegacyBackend(str(journal_env))
    adapter = LegacyAdapter(backend)
    app = _SessionSwitchApp(TuiOptions(), backend=backend)
    try:
        old_session = adapter._adopt_session({
            "session_id": "ses_old", "workspace_id": "ws_rotation"})
        app._backend = backend
        app._adapter = adapter
        app._adapter_session_id = old_session.session_id
        app._enable_runtime_projection()
        old_controller = app._event_controller
        fixed = uuid.UUID("11111111-1111-1111-1111-111111111111")
        target_session_id = f"ses_{fixed.hex}"
        monkeypatch.setattr("pbatch.tui.uuid.uuid4", lambda: fixed)
        RuntimeEventJournal(
            str(journal_env), target_session_id).mark_read_only(
                "target must be reconciled")

        app._new_session("must-not-start")

        assert backend.session_id == "provider-old"
        assert backend.new_session_calls == []
        assert adapter._session is old_session
        assert app._adapter_session_id == old_session.session_id
        assert app._journal_session_id == old_session.session_id
        assert app._event_controller is old_controller
        assert not app._runtime_blocked_reason
        assert any("journal recovery is read-only" in entry.text
                   for entry in app._entries if entry.role == "error")
    finally:
        adapter.close()
        app._event_controller.close()


def test_new_session_rotates_past_read_only_legacy_pending_without_cross_write(
        journal_env):
    backend = _LegacyBackend(str(journal_env))
    adapter = LegacyAdapter(backend)
    try:
        old_session = adapter._adopt_session({
            "session_id": "ses_old", "workspace_id": "ws_rotation"})
        harness = _ProjectionHarness(backend, adapter, old_session.session_id)
        harness._enable_runtime_projection()
        old_controller = harness._event_controller
        old_journal = old_controller._journal
        assert old_journal is not None
        old_journal.mark_read_only("simulated permanent failure")
        with pytest.raises(RuntimeEventJournalError, match="read-only"):
            old_controller.ingest(_event(
                1, "session.disconnected", event_id="evt_latch_old",
                run="run_latch_old", turn=""))

        adapter._publish(
            "session.disconnected", session=old_session,
            run_id="run_pending_old", turn_id="",
            payload={"state": "DISCONNECTED"})
        harness._prepare_runtime_session_switch()
        harness._preflight_runtime_session("ses_new")

        assert harness._quarantined_runtime_event_count == 1
        assert harness._quarantined_runtime_events[-1].run_id == "run_pending_old"
        old_recovery = RuntimeEventJournal(
            str(journal_env), "ses_old").recover()
        assert all(event.run_id != "run_pending_old"
                   for event in old_recovery.events)
        new_session = adapter.start_session({
            "session_id": "ses_new", "workspace_id": "ws_rotation",
            "name": "replacement"})
        harness._adapter_session_id = new_session.session_id

        harness._prepare_runtime_projection()

        assert harness._event_controller is not old_controller
        assert harness._journal_session_id == "ses_new"
        assert not harness._runtime_blocked_reason
        recovered = RuntimeEventJournal(
            str(journal_env), "ses_new").recover()
        assert any(event.kind == "session.created"
                   for event in recovered.events)
        assert recovered.events
        assert {event.session_id for event in recovered.events} == {"ses_new"}
        assert all(event.run_id != "run_pending_old"
                   for event in recovered.events)
    finally:
        adapter.close()


def test_session_switch_routes_writable_old_pending_only_to_old_journal(
        journal_env):
    backend = _LegacyBackend(str(journal_env))
    adapter = LegacyAdapter(backend)
    try:
        old_session = adapter._adopt_session({
            "session_id": "ses_old", "workspace_id": "ws_rotation"})
        harness = _ProjectionHarness(backend, adapter, old_session.session_id)
        harness._enable_runtime_projection()
        old_controller = harness._event_controller
        adapter._publish(
            "session.disconnected", session=old_session,
            run_id="run_pending_old", turn_id="",
            payload={"state": "DISCONNECTED"})

        harness._prepare_runtime_session_switch()

        old_recovery = RuntimeEventJournal(
            str(journal_env), "ses_old").recover()
        assert any(event.run_id == "run_pending_old"
                   for event in old_recovery.events)
        assert harness._quarantined_runtime_event_count == 0
        harness._preflight_runtime_session("ses_new")
        new_session = adapter.start_session({
            "session_id": "ses_new", "workspace_id": "ws_rotation",
            "name": "replacement"})
        harness._adapter_session_id = new_session.session_id
        harness._prepare_runtime_projection()

        assert harness._event_controller is not old_controller
        assert harness._journal_session_id == "ses_new"
        new_recovery = RuntimeEventJournal(
            str(journal_env), "ses_new").recover()
        assert any(event.kind == "session.created"
                   for event in new_recovery.events)
        assert all(event.session_id == "ses_new"
                   for event in new_recovery.events)
        assert all(event.run_id != "run_pending_old"
                   for event in new_recovery.events)
    finally:
        adapter.close()
