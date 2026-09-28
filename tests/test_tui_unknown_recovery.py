"""Unconfirmed saved outcomes cannot gain execution authority after restart."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_backend import TUIBackend
from pbatch.tui_controller import TUIEventController
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_protocol import RuntimeEvent, TurnState
from pbatch.tui_sessions import recover_resume_target


UNKNOWN_SOURCES = ("QUEUED", "RUNNING", "INTERRUPTING", "UNKNOWN")


def _event(cwd, session, seq, kind, payload=None, turn="old-turn"):
    return RuntimeEvent(
        event_id=f"evt_{session}_{seq}", seq=seq, occurred_at="2026-09-27T00:00:00Z",
        workspace_id=str(cwd), session_id=session, run_id="saved-run",
        turn_id="" if kind.startswith("session.") else turn, actor_id="agent_main",
        kind=kind, payload=payload or {}, extensions={"runtime": "codex-exec"})


def _journal(cwd, state):
    journal = RuntimeEventJournal(cwd, "saved-" + state.lower())
    writer = TUIEventController(journal=journal)
    events = [("session.created", {"provider_session_id": "old-provider-thread"}),
              ("turn.queued", {"state": "QUEUED"})]
    if state != "QUEUED":
        events.append(("turn.started", {"state": "RUNNING"}))
    if state == "INTERRUPTING":
        events.append(("turn.interrupting", {"state": state}))
    elif state in ("UNKNOWN", "SUCCEEDED"):
        kind = "turn.failed" if state == "UNKNOWN" else "turn.completed"
        events.append((kind, {"state": state}))
    for seq, (kind, payload) in enumerate(events, 1):
        writer.ingest(_event(cwd, journal.session_id, seq, kind, payload))
    writer.close()
    return journal


def _app(cwd, monkeypatch):
    backend = TUIBackend(cwd=str(cwd), agent="codex", memory_mode="off", session_id="current")
    app = PiBatchTUI(TuiOptions(cwd=str(cwd)), backend=backend)
    app._structured_backend = app._projection_driven = True
    for name in ("_refresh_status", "_show_recovered_runtime", "_reset_approval_controls",
                 "_reset_follow_ups_for_session_switch", "_append_entry"):
        monkeypatch.setattr(app, name, lambda *_a, **_kw: None)
    monkeypatch.setattr(app, "_apply_runtime_snapshot", lambda view: setattr(app, "_runtime_view", view))
    monkeypatch.setattr(backend, "run_turn_structured", lambda *_a: pytest.fail("unexpected provider turn"))
    return app, backend


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    return cwd


@pytest.mark.parametrize("state", (*UNKNOWN_SOURCES, "SUCCEEDED"))
def test_replayed_unknown_is_read_only_before_real_session_resume(workspace, monkeypatch, state):
    journal = _journal(workspace, state)
    original = journal.path.read_bytes()
    app, backend = _app(workspace, monkeypatch)
    recovered = TUIEventController(journal=journal)
    unknown = state != "SUCCEEDED"
    assert recovered.snapshot().turn_state == (TurnState.UNKNOWN if unknown else TurnState.SUCCEEDED)
    assert recovered.recovery.read_only is unknown
    target = recover_resume_target(recovered.recovery.journal)
    assert app._resume_execution_mode(target, recovered) == ("journal-read-only" if unknown else "executable")
    recovered.close()
    app._resume_tui_session(journal.session_id)
    assert bool(app._runtime_blocked_reason) is unknown
    assert backend.provider_resume_required is not unknown
    assert app._prepare_runtime_action() is not unknown
    assert not app._busy
    assert journal.path.read_bytes() == original
    app._event_controller.close()


@pytest.mark.parametrize("state", UNKNOWN_SOURCES)
def test_new_session_executes_without_reauthorizing_unknown_history(workspace, monkeypatch, state):
    journal = _journal(workspace, state)
    original = journal.path.read_bytes()
    app, backend = _app(workspace, monkeypatch)
    app._resume_tui_session(journal.session_id)
    assert not app._prepare_runtime_action()
    app._new_session()
    assert app._prepare_runtime_action()
    assert not app._runtime_blocked_reason and not app._busy
    assert backend.session_id != journal.session_id
    assert not backend.provider_resume_required
    for seq, (kind, value) in enumerate((("turn.started", "RUNNING"), ("turn.completed", "SUCCEEDED")), 1):
        app._event_controller.ingest(_event(workspace, backend.session_id, seq, kind,
                                           {"state": value}, turn="new-turn"))
    assert app._event_controller.snapshot().turn_state == TurnState.SUCCEEDED
    app._event_controller.close()
    old = TUIEventController(journal=journal)
    assert old.recovery.read_only and old.snapshot().turn_state == TurnState.UNKNOWN
    assert journal.path.read_bytes() == original
    old.close()


@pytest.mark.parametrize("cache", ["missing", "forged"])
def test_unknown_authority_comes_from_journal_not_summary_cache(workspace, cache):
    journal = _journal(workspace, "UNKNOWN")
    original = journal.path.read_bytes()
    if cache == "missing":
        journal.summary_path.unlink()
    else:
        summary = json.loads(journal.summary_path.read_text())
        summary.update(read_only=False, requires_reconciliation=False, turn_state="SUCCEEDED")
        journal.summary_path.write_text(json.dumps(summary))
    recovered = TUIEventController(journal=journal)
    assert recovered.recovery.read_only
    assert recovered.recovery.unresolved_state == TurnState.UNKNOWN
    assert recovered.snapshot().turn_state == TurnState.UNKNOWN
    assert journal.path.read_bytes() == original
    recovered.close()


def test_old_unknown_remains_read_only_after_projection_eviction(workspace):
    journal = _journal(workspace, "UNKNOWN")
    for seq, kind in ((5, "turn.started"), (6, "turn.completed")):
        journal.append(_event(workspace, journal.session_id, seq, kind, turn="later-turn"))
    original = journal.path.read_bytes()
    recovered = TUIEventController(journal=journal, entity_limit=1)
    assert "old-turn" not in recovered.projection.turns
    assert recovered.snapshot().turn_state == TurnState.SUCCEEDED
    assert recovered.recovery.read_only
    assert "UNKNOWN" in recovered.recovery.unresolved_state
    assert journal.path.read_bytes() == original
    recovered.close()


def test_late_unknown_cannot_undo_confirmed_success(workspace):
    journal = _journal(workspace, "SUCCEEDED")
    journal.append(_event(workspace, journal.session_id, 5, "turn.failed", {"state": "UNKNOWN"}))
    recovered = TUIEventController(journal=journal)
    assert recovered.snapshot().turn_state == TurnState.SUCCEEDED
    assert not recovered.recovery.read_only
    assert recovered.recovery.unresolved_state == ""
    recovered.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PTY process evidence")
def test_real_pty_recovery_blocks_unknown_and_allows_new_and_succeeded(tmp_path):
    script = Path(__file__).resolve().parents[1] / "tools" / "smoke-tui-recovery.py"
    result = subprocess.run([sys.executable, str(script), "--output-dir", "recovery-evidence"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=70)
    report = json.loads((tmp_path / "recovery-evidence" / "report.json").read_text())
    assert result.returncode == 0, (result.stdout, result.stderr, report)
    assert report["ok"] and len(report["cases"]) == 5
    assert all(case["provider_prompts"] == case["provider_switches"] == 0
               for case in report["cases"][:-1])
    assert report["cases"][-1]["resumed_successfully"]
