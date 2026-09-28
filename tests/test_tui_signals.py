"""OS shutdown must reap owned providers without inventing successful turns."""

import json
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_controller import TUIEventController
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_protocol import RuntimeEvent, TurnState
from pbatch.tui_signals import _exit_after_signal, tui_signal_handlers


pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX shutdown signals")


def test_signal_handlers_queue_once_and_restore_after_failed_startup():
    pending = []
    app = SimpleNamespace(call_later=lambda *args: pending.append(args))
    inherited = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
    with pytest.raises(RuntimeError, match="startup failed"):
        with tui_signal_handlers(app) as stopped:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
            assert len(pending) == 1
            assert pending[0] == (_exit_after_signal, app, signal.SIGTERM)
            assert stopped.return_code == 143
            raise RuntimeError("startup failed")
    assert all(signal.getsignal(number) is handler for number, handler in inherited.items())


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)))
    journal = RuntimeEventJournal(tmp_path, "signal-session")
    app._event_controller = TUIEventController(journal=journal)
    app._projection_driven = True
    monkeypatch.setattr(app, "_apply_runtime_snapshot",
                        lambda view: setattr(app, "_runtime_view", view))
    monkeypatch.setattr(app, "_append_entry", lambda *_a, **_kw: None)
    monkeypatch.setattr(app, "action_quit", lambda: None)
    return app, journal


def _event(tmp_path, seq, kind, state):
    return RuntimeEvent(
        event_id=f"evt_signal_{seq}", seq=seq, occurred_at="2026-09-26T00:00:00Z",
        workspace_id=str(tmp_path), session_id="signal-session", run_id="signal-run",
        turn_id="signal-turn", actor_id="agent_main", kind=kind, payload={"state": state})


@pytest.mark.parametrize("completed", [False, True])
def test_signal_journal_recovers_unknown_without_rewriting_known_completion(
        tmp_path, monkeypatch, completed):
    app, journal = _app(tmp_path, monkeypatch)
    app._ingest_runtime_event(_event(tmp_path, 1, "turn.started", TurnState.RUNNING))
    if completed:
        # Deliberately leave the UI snapshot stale while the controller has
        # accepted completion, as when a worker callback is still queued.
        app._event_controller.ingest(_event(tmp_path, 2, "turn.completed", TurnState.SUCCEEDED))
    prefix = journal.path.read_bytes()
    _exit_after_signal(app, signal.SIGTERM)
    assert journal.path.read_bytes().startswith(prefix)
    app._event_controller.close()
    recovered = TUIEventController(journal=journal)
    expected = TurnState.SUCCEEDED if completed else TurnState.UNKNOWN
    assert recovered.snapshot().turn_state == expected
    kinds = [event.kind for event in journal.recover().events]
    assert kinds == ["turn.started", "turn.completed" if completed else "turn.failed"]
    if not completed:
        saved = journal.path.read_bytes()
        with pytest.raises(RuntimeError, match="reconciliation"):
            recovered.ingest(_event(tmp_path, 100, "turn.completed", TurnState.SUCCEEDED))
        assert recovered.snapshot().turn_state == TurnState.UNKNOWN
        assert journal.path.read_bytes().startswith(saved)
        replayed = TUIEventController(journal=journal)
        assert replayed.snapshot().turn_state == TurnState.UNKNOWN
        replayed.close()
    recovered.close()


def test_signal_cannot_mark_an_attached_external_pipeline_failed(tmp_path, monkeypatch):
    app, journal = _app(tmp_path, monkeypatch)
    app._ingest_runtime_event(_event(tmp_path, 1, "turn.started", TurnState.RUNNING))
    app._native_pipeline_attachment = object()
    prefix = journal.path.read_bytes()
    _exit_after_signal(app, signal.SIGHUP)
    assert journal.path.read_bytes() == prefix
    app._event_controller.close()


def test_signal_during_unacknowledged_interrupt_remains_unknown(tmp_path, monkeypatch):
    app, journal = _app(tmp_path, monkeypatch)
    app._ingest_runtime_event(_event(tmp_path, 1, "turn.started", TurnState.RUNNING))
    app._ingest_runtime_event(_event(tmp_path, 2, "turn.interrupting", TurnState.INTERRUPTING))
    _exit_after_signal(app, signal.SIGTERM)
    assert app._event_controller.snapshot().turn_state == TurnState.UNKNOWN
    assert journal.recover().events[-1].payload["state"] == TurnState.UNKNOWN
    app._event_controller.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PTY process evidence")
def test_real_pty_signals_reap_active_provider_and_descendant(tmp_path):
    root = Path(__file__).resolve().parents[1]
    evidence = tmp_path / "signal-evidence"
    result = subprocess.run(
        [sys.executable, str(root / "tools" / "smoke-tui-signals.py"),
         "--output-dir", evidence.name], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    report = json.loads((evidence / "report.json").read_text())
    assert result.returncode == 0, (result.stdout, result.stderr, report)
    assert report["ok"] and len(report["cases"]) == 3
    assert [case["exit_code"] for case in report["cases"]] == [0, 143, 129]
    assert all(not case["live_owned_processes"] for case in report["cases"])
    assert all(case["terminal_states"] == ["UNKNOWN"] for case in report["cases"][1:])
