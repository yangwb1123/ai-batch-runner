"""Public backend tracking must not convert a refused required fold into admission."""
from __future__ import annotations

from dataclasses import replace

import pytest

from pbatch.tui_adapter_codex import CodexExecAdapter
from pbatch.tui_backend import TUIBackend
from pbatch.tui_protocol import RuntimeEvent
from test_tui_adapter_codex import FakeProc, FakeSpawn
from test_tui_codex_startup_diagnostics import _reply_lines


@pytest.fixture
def backend(tmp_path):
    return TUIBackend(cwd=str(tmp_path), agent='codex', memory_mode='off', timeout=5)


@pytest.fixture
def event():
    return RuntimeEvent(event_id='evt_owned_admission', seq=1, occurred_at='now',
        workspace_id='owned-ws', session_id='owned-session', run_id='owned-run',
        turn_id='owned-turn', actor_id='agent_main', kind='runtime.warning',
        payload={'text': 'owned diagnostic'})


@pytest.mark.parametrize('receipt', [True, False, None])
def test_tracker_preserves_receipt_and_only_tracks_admitted_context(backend, event, receipt):
    backend.set_runtime_event_sink(lambda _event: receipt)
    context = {'turn_id': 'previous-turn'}
    sink = backend._tracked_event_sink(context)
    assert sink(event) is receipt
    assert context['turn_id'] == ('previous-turn' if receipt is False else event.turn_id)
    assert ('session_id' in context) is (receipt is not False)


def test_required_fold_exception_does_not_replace_owned_context(backend, event):
    def reject(_event):
        raise RuntimeError('owned required fold failure')
    backend.set_runtime_event_sink(reject)
    context = {'turn_id': 'previous-turn'}
    sink = backend._tracked_event_sink(context)
    with pytest.raises(RuntimeError, match='owned required fold failure'):
        sink(event)
    assert context == {'turn_id': 'previous-turn'}


def test_detached_required_sink_does_not_become_legacy_void_admission(backend, event):
    backend.set_runtime_event_sink(lambda _event: True)
    context = {}
    sink = backend._tracked_event_sink(context)
    backend.set_runtime_event_sink(None)
    assert sink(event) is False and context == {}


def test_initially_headless_backend_keeps_optional_sink_behavior(backend):
    assert backend._tracked_event_sink({}) is None


def test_workspace_snapshot_uses_admitted_context_not_later_refused_event(backend, event):
    seen = []
    def admit_first(row):
        seen.append(row)
        return row is event
    backend.set_runtime_event_sink(admit_first)
    context = {}
    sink = backend._tracked_event_sink(context)
    assert sink(event) is True
    assert sink(replace(event, turn_id='refused-turn', run_id='refused-run')) is False
    backend._publish_workspace_snapshot(sink, context, {})
    assert seen[-1].kind == 'workspace.snapshot'
    assert seen[-1].turn_id == event.turn_id and seen[-1].run_id == event.run_id


def test_public_backend_fails_closed_when_required_projection_refuses_terminal(backend):
    backend.set_runtime_event_sink(lambda event: event.kind != 'turn.completed')
    process = FakeProc(_reply_lines())
    result = backend.run_turn_structured('owned synthetic turn', adapter_factory=(
        lambda cwd: CodexExecAdapter(cwd, spawn=FakeSpawn(process))))
    assert not result.success and result.stdout == 'GOT_IT'
    assert result.reason == 'runtime terminal event was not admitted by required projection'
