"""Fresh required-fold and result-display regression, using only owned fixtures."""
from dataclasses import replace
from types import SimpleNamespace
import queue
import threading

import pytest

from pbatch import tui_commands as commands
from pbatch import tui_lifecycle as lifecycle
from pbatch.tui_projection_ui import TUIProjectionMixin
from pbatch.tui_protocol import RuntimeEvent, TurnState
from pbatch.tui_review import AppServerReviewCapture
from pbatch.tui_runtime import TurnHandle
from test_tui_structured_admission import Timer


class App(TUIProjectionMixin):
    def __init__(self):
        self._ui_thread_id = threading.get_ident()
        self._runtime_blocked_reason = ''
        self._init_runtime_projection()

    def _apply_runtime_snapshot(self, view):
        self._runtime_view = view


class Stream:
    def __init__(self, rows):
        self.rows = list(rows)

    def get(self, timeout=None):
        if not self.rows:
            raise queue.Empty
        return self.rows.pop(0)


def rows(kind='turn.completed'):
    values = [('turn.queued', {'state': TurnState.QUEUED}),
              ('turn.started', {'state': TurnState.RUNNING}),
              ('message.completed', {'text': 'OWNED CANONICAL ANSWER', 'item_id': 'owned_msg'})]
    if kind == 'turn.interrupted':
        values.append(('turn.interrupting', {'state': TurnState.INTERRUPTING}))
    state = {'turn.completed': TurnState.SUCCEEDED, 'turn.failed': TurnState.FAILED,
             'turn.interrupted': TurnState.INTERRUPTED}[kind]
    values.append((kind, {'state': state}))
    return [RuntimeEvent(event_id=f'evt_fresh_owned_{n}', seq=n, occurred_at='now',
        workspace_id='owned-ws', session_id='owned-session', run_id='owned-run',
        turn_id='owned-turn', actor_id='agent_main', kind=name, payload=payload)
        for n, (name, payload) in enumerate(values, 1)]


def setup(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(lifecycle, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(lifecycle.threading, 'Timer', Timer)
    closed = threading.Event()
    adapter = SimpleNamespace(close=closed.set)
    operation = lifecycle.RuntimeOperation(adapter, 3, 'owned turn')
    handle = TurnHandle('owned-session', 'owned-run', 'owned-turn', 'agent_main', 'now')
    operation.bind(handle)
    app = App()
    app._runtime_operation = operation
    return app, operation, handle, clock, closed


def consume(app, handle, events, mode):
    if mode == 'managed':
        return lifecycle.TUIRuntimeLifecycleMixin._consume_adapter_turn(
            app, Stream(events), handle, app._runtime_operation)
    return commands.TUIReviewMixin._consume_appserver_review(
        app, Stream(events), handle, AppServerReviewCapture())


@pytest.mark.parametrize('mode', ['managed', 'review'])
@pytest.mark.parametrize('kind', ['turn.completed', 'turn.failed', 'turn.interrupted'])
def test_terminal_deadline_is_checked_after_required_fold(monkeypatch, mode, kind):
    app, operation, handle, clock, closed = setup(monkeypatch)
    original = app._event_controller.ingest

    def fold(row):
        view = original(row)
        if row.kind == kind:
            clock[0] = 103.0
        return view

    monkeypatch.setattr(app._event_controller, 'ingest', fold)
    try:
        with pytest.raises(lifecycle.RuntimeOperationStopped, match='timed out after 3s'):
            consume(app, handle, rows(kind), mode)
        assert not operation._finished and operation.deadline == 103 and closed.wait(1)
        assert app._event_controller.snapshot().turn_state == rows(kind)[-1].payload['state']
        assert app._event_controller.snapshot().assistant_text == 'OWNED CANONICAL ANSWER'
    finally:
        operation.finish()
        app._event_controller.close()


@pytest.mark.parametrize('mode', ['managed', 'review'])
def test_duplicate_terminal_is_not_raw_completion(monkeypatch, mode):
    app, operation, handle, _clock, _closed = setup(monkeypatch)
    events = rows()
    events[-1] = replace(events[-1], event_id=events[1].event_id)
    try:
        with pytest.raises(RuntimeError, match='not admitted by required projection'):
            consume(app, handle, events, mode)
        assert not operation._finished
        assert app._event_controller.snapshot().turn_state == TurnState.RUNNING
    finally:
        operation.finish()
        app._event_controller.close()


def test_ingress_receipt_reports_fold_not_presentation(monkeypatch):
    app, operation, _handle, _clock, _closed = setup(monkeypatch)
    app._ui_thread_id = -1
    monkeypatch.setattr(app, 'call_from_thread', lambda *_args: (_ for _ in ()).throw(
        RuntimeError('OWNED PRESENTATION UNAVAILABLE')), raising=False)
    try:
        assert app._ingest_runtime_event(rows()[0]) is True
        assert app._ingest_runtime_event(rows()[0]) is False
        assert app._event_controller.snapshot().turn_state == TurnState.QUEUED
    finally:
        operation.finish()
        app._event_controller.close()


def test_review_does_not_capture_filtered_message(monkeypatch):
    app, operation, handle, _clock, _closed = setup(monkeypatch)
    events = rows()
    rejected = replace(events[2], seq=4, payload={'item_id': 'refused', 'text': 'OWNED REFUSED RAW BODY'})
    events.insert(-1, rejected)
    events[-1] = replace(events[-1], seq=5)
    captured = []
    monkeypatch.setattr(commands, 'capture_runtime_review_event', lambda _capture, row: captured.append(row))
    try:
        consume(app, handle, events, 'review')
        assert rejected not in captured
        assert operation._finished and app._event_controller.snapshot().assistant_text == 'OWNED CANONICAL ANSWER'
    finally:
        operation.finish()
        app._event_controller.close()


def test_review_capture_work_is_required_before_terminal_admission(monkeypatch):
    app, operation, handle, clock, closed = setup(monkeypatch)

    def capture(_capture, row):
        if row.kind == 'turn.completed':
            assert not operation._finished
            clock[0] = 103.0

    monkeypatch.setattr(commands, 'capture_runtime_review_event', capture)
    try:
        with pytest.raises(lifecycle.RuntimeOperationStopped, match='timed out after 3s'):
            consume(app, handle, rows(), 'review')
        assert not operation._finished and closed.wait(1)
        assert app._event_controller.snapshot().turn_state == TurnState.SUCCEEDED
    finally:
        operation.finish()
        app._event_controller.close()


@pytest.mark.parametrize('finish', ['result', 'error'])
def test_provider_success_does_not_render_ready_over_local_failure(finish):
    from pbatch.tui import PiBatchTUI
    view = SimpleNamespace(turn_state=TurnState.SUCCEEDED)
    harness = SimpleNamespace(_runtime_view=view, _turn_stderr_seen=False, _turn_stdout_seen=False,
        _reconcile_runtime_failure=lambda _reason: False, _append_active=lambda *_args: None,
        _projected_terminal_label=lambda: 'Ready')
    if finish == 'result':
        PiBatchTUI._finish_turn_result(harness, {'success': False, 'reason': 'OWNED LOCAL REFUSAL'})
    else:
        PiBatchTUI._finish_turn_error(harness, RuntimeError('OWNED LOCAL REFUSAL'))
    assert harness._last_status == 'Failed (local acceptance)'
    assert view.turn_state == TurnState.SUCCEEDED
