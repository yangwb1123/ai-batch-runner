"""Explicit session recovery preserves an unconfirmed turn after storage faults."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_controller import TUIEventController
from pbatch.tui_protocol import TurnState
from pbatch.tui_review import parse_review_args
from test_tui_active_journal_failures import (
    _Backend, _Runtime, _fail_metadata_after_started, _fail_started_writes,
)


def _recovery_app(workspace, monkeypatch, consumer):
    backend = _Backend(workspace)
    runtimes = [_Runtime(workspace), _Runtime(workspace)]
    available = iter(runtimes)
    app = PiBatchTUI(TuiOptions(
        cwd=str(workspace), timeout=5,
        runtime="legacy" if consumer == "legacy" else "app-server"),
        backend=backend, appserver_factory=Mock(side_effect=lambda cwd: next(available)))
    # Exercise real adapters, journals and lifecycle synchronously; presentation
    # has independent run_test coverage in test_tui_active_journal_failures.
    for method in ("_refresh_status", "_reset_approval_controls",
                   "_invalidate_session_controls", "_reset_follow_ups_for_session_switch"):
        monkeypatch.setattr(app, method, lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "_session_control_busy", lambda: False)
    monkeypatch.setattr(app, "_append_entry", lambda role, text, **kwargs:
                        app._entries.append(SimpleNamespace(role=role, text=text)))
    monkeypatch.setattr(app, "_apply_runtime_snapshot",
                        lambda view: setattr(app, "_runtime_view", view))
    monkeypatch.setattr(app, "call_from_thread", lambda callback, *args: callback(*args))
    app._attach_adapter()
    return app, backend, runtimes


def _execute(app, consumer, output):
    assert app._prepare_runtime_action(review=consumer == "review")
    app._begin_runtime_operation("recovery test")
    error = None
    if consumer == "review":
        _, _, error = app._run_appserver_review(parse_review_args(()), output)
    else:
        try:
            app._run_adapter_turn(app._adapter, app._runtime_operation, "explicit task")
        except Exception as exc:
            error = exc
    error = app._finish_runtime_operation(app._runtime_operation, error)
    if error is not None:
        app._reconcile_runtime_failure(str(error))
    return error


def _assert_recovered_history(journal, prefix):
    assert journal.path.read_bytes().startswith(prefix)
    recovered = TUIEventController(journal=journal)
    try:
        assert recovered.snapshot().turn_state == TurnState.UNKNOWN
        failures = [event for event in journal.recover().events
                    if event.kind == "turn.failed"]
        assert len(failures) == 1
        assert failures[0].payload["state"] == TurnState.UNKNOWN
    finally:
        recovered.close()


@pytest.mark.parametrize("consumer", ["legacy", "turn", "review"])
@pytest.mark.parametrize("fault", ["queued-persistent-enospc", "running-permission"])
def test_new_saves_pending_failure_before_late_events(
        tmp_path, monkeypatch, consumer, fault):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app, backend, runtimes = _recovery_app(workspace, monkeypatch, consumer)
    journal = app._event_controller._journal
    try:
        with monkeypatch.context() as patch:
            if fault == "running-permission":
                _fail_metadata_after_started(patch, journal)
            else:
                _fail_started_writes(patch, journal, persistent=True)
            assert _execute(app, consumer, tmp_path / "review.json") is not None
            pending = app._pending_runtime_failure
            assert pending is not None
            prefix = journal.path.read_bytes()
            adapter = app._adapter
            connections = app._appserver_factory.call_count
            for _ in range(2):
                app._new_session()
                assert app._adapter is adapter
                assert app._appserver_factory.call_count == connections
                assert app._journal_session_id == journal.session_id
                assert app._pending_runtime_failure is pending
                assert app._runtime_blocked_reason
        app._new_session()
        assert not app._runtime_blocked_reason
        assert app._pending_runtime_failure is None
        assert app._journal_session_id != journal.session_id
        assert len(backend.prompts) + sum(len(runtime.requests) for runtime in runtimes) == 1
        _assert_recovered_history(journal, prefix)
        assert _execute(app, consumer, tmp_path / "fresh-review.json") is None
        assert app._runtime_view.turn_state == TurnState.SUCCEEDED
        assert len(backend.prompts) + sum(len(runtime.requests) for runtime in runtimes) == 2
        _assert_recovered_history(journal, prefix)
    finally:
        app._adapter.close()
        app._event_controller.close()
