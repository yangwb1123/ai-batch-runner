"""Fresh, provider-free reproduction after prior temporary evidence went missing."""
from types import SimpleNamespace
import threading

import pytest

from pbatch import tui_backend_structured as structured
from pbatch import tui_lifecycle as lifecycle
from pbatch.models import Task


class Clock:
    now = 100.0

    def monotonic(self):
        return self.now


class Timer:
    instances = []

    def __init__(self, delay, callback):
        self.delay, self.callback, self.cancelled = delay, callback, False
        self.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


class InlineThread:
    after_consume = None

    def __init__(self, target, args=(), **_kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)
        if self.target is structured._consume_events and type(self).after_consume:
            type(self).after_consume()

    def join(self, timeout=None):
        pass


class Adapter:
    def __init__(self, clock, events=None, phase='', terminal_at=100.1):
        self.clock, self.phase, self.terminal_at = clock, phase, terminal_at
        self.observations = events
        self.calls, self.closed = [], False
        self.handle = SimpleNamespace(turn_id='owned_turn', done=False)

    def _phase(self, name):
        self.calls.append(name)
        if self.phase == name:
            self.clock.now = 103.0

    def initialize(self, _request):
        self._phase('initialize')

    def start_session(self, _request):
        self._phase('session')

    def start_turn(self, _request):
        self._phase('start')
        return self.handle

    def events(self):
        self.calls.append('consume')
        rows = self.observations if self.observations is not None else [
            event('message.completed', item_id='owned_message', text='OWNED ADMITTED ANSWER'),
            event('turn.completed')]
        for row in rows:
            if row.kind.startswith('turn.'):
                self.clock.now = max(self.clock.now, self.terminal_at)
            yield row
        self.handle.done = True

    def interrupt_turn(self, _request):
        self.calls.append('interrupt')

    def close(self):
        self.closed = True
        self.calls.append('close')


def event(kind, **payload):
    return SimpleNamespace(kind=kind, payload=payload)


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(structured, 'time', value)
    monkeypatch.setattr(lifecycle, 'time', value)
    monkeypatch.setattr(structured, 'threading', SimpleNamespace(
        Thread=InlineThread, Event=threading.Event, Lock=threading.Lock))
    monkeypatch.setattr(lifecycle, 'threading', SimpleNamespace(
        Timer=Timer, Thread=InlineThread, Event=threading.Event, RLock=threading.RLock))
    monkeypatch.setattr(InlineThread, 'after_consume', None)
    monkeypatch.setattr(Timer, 'instances', [])
    return value


def invoke(adapter, sink=None, notify=lambda *_args: None, cancelled=lambda: False):
    backend = SimpleNamespace(cwd='owned-workspace', timeout=3,
                              session_id='owned-session', session_name='owned')
    return structured._invoke_structured(backend, Task(prompt='owned synthetic task'), 1,
        lambda: adapter, lambda _interrupt: None, notify, cancelled, sink)


@pytest.mark.parametrize('terminal_at', [103.0, 103.001])
def test_already_done_stream_cannot_accept_late_terminal(clock, terminal_at):
    adapter = Adapter(clock, terminal_at=terminal_at)
    result = invoke(adapter)
    assert not result.success and result.reason == 'agent timed out after 3s'
    assert result.stdout == 'OWNED ADMITTED ANSWER' and adapter.closed


@pytest.mark.parametrize('phase', ['initialize', 'session', 'start'])
def test_startup_budget_is_not_reset_or_ignored(clock, phase):
    adapter = Adapter(clock, phase=phase)
    result = invoke(adapter)
    assert not result.success and result.reason == 'agent timed out after 3s'
    assert 'consume' not in adapter.calls and adapter.closed
    if phase == 'initialize':
        assert 'session' not in adapter.calls
    if phase != 'start':
        assert 'start' not in adapter.calls


@pytest.mark.parametrize('kind', ['turn.completed', 'turn.failed', 'turn.interrupted'])
@pytest.mark.parametrize('finished_at', [103.0, 103.001])
def test_required_terminal_fold_must_finish_before_original_deadline(clock, kind, finished_at):
    adapter = Adapter(clock, events=[event('message.completed', text='OWNED ADMITTED ANSWER'), event(kind)])
    facts = []

    def sink(row):
        facts.append(row.kind)
        if row.kind == kind:
            clock.now = finished_at
        return True

    result = invoke(adapter, sink)
    assert not result.success and result.reason == 'agent timed out after 3s'
    assert facts[-1] == kind and result.stdout == 'OWNED ADMITTED ANSWER'
    assert adapter.closed


@pytest.mark.parametrize('kind', ['turn.completed', 'turn.failed', 'turn.interrupted'])
def test_refused_terminal_is_not_raw_stream_success(clock, kind):
    adapter = Adapter(clock, events=[event(kind)])
    result = invoke(adapter, sink=lambda _row: False)
    assert not result.success and result.reason == (
        'runtime terminal event was not admitted by required projection')
    assert adapter.closed and not result.stdout


@pytest.mark.parametrize('kind', ['message.delta', 'message.completed', 'runtime.warning', 'runtime.error'])
def test_refused_nonterminal_body_never_reenters_result(clock, kind):
    refused = event(kind, item_id='refused', text='OWNED REFUSED RAW BODY')
    adapter = Adapter(clock, events=[refused,
        event('message.completed', item_id='accepted', text='OWNED ADMITTED ANSWER'), event('turn.completed')])
    result = invoke(adapter, sink=lambda row: row is not refused)
    assert result.success and result.stdout == 'OWNED ADMITTED ANSWER'
    assert not result.stderr and adapter.closed


@pytest.mark.parametrize('receipt', [True, None, 0])
def test_legacy_void_and_non_false_return_behavior_is_retained(clock, receipt):
    result = invoke(Adapter(clock), sink=lambda _row: receipt)
    assert result.success and result.stdout == 'OWNED ADMITTED ANSWER'


def test_no_sink_preserves_existing_presentation(clock):
    notices = []
    result = invoke(Adapter(clock), notify=lambda *notice: notices.append(notice))
    assert result.success and notices == [('stdout', 'OWNED ADMITTED ANSWER')]


def test_refused_start_does_not_use_existing_item_budget(clock):
    rows = [event('message.started', item_id=f'refused_{n}') for n in range(257)]
    rows.extend([event('message.completed', item_id='accepted', text='OWNED ADMITTED ANSWER'), event('turn.completed')])
    result = invoke(Adapter(clock, events=rows), sink=lambda row: row.kind != 'message.started')
    assert result.success and result.stdout == 'OWNED ADMITTED ANSWER'


def test_refused_complete_does_not_replace_admitted_partial(clock):
    refused = event('message.completed', item_id='accepted', text='OWNED REFUSED REPLACEMENT')
    rows = [event('message.delta', item_id='accepted', text='OWNED ADMITTED ANSWER'), refused, event('turn.completed')]
    result = invoke(Adapter(clock, events=rows), sink=lambda row: row is not refused)
    assert result.success and result.stdout == 'OWNED ADMITTED ANSWER'


def test_filtered_only_stream_remains_failed_missing_terminal(clock):
    result = invoke(Adapter(clock, events=[event('message.completed', text='OWNED REFUSED')]),
                    sink=lambda _row: False)
    assert not result.success and not result.stdout and not result.streamed_output
    assert result.reason == 'structured runtime event stream closed without a terminal event'


def test_filtered_work_cannot_extend_original_deadline(clock):
    facts = []

    def sink(row):
        facts.append(row.kind)
        clock.now = 103.0
        return False

    result = invoke(Adapter(clock), sink)
    assert not result.success and result.reason == 'agent timed out after 3s'
    assert facts == ['message.completed'] and not result.stdout


def test_timely_terminal_survives_late_coordinator_and_watchdog(clock, monkeypatch):
    adapter = Adapter(clock, terminal_at=102.999)

    def wake_late():
        clock.now = 108.0
        Timer.instances[0].callback()

    monkeypatch.setattr(InlineThread, 'after_consume', wake_late)
    result = invoke(adapter)
    assert result.success and result.elapsed == 8.0 and not result.reason
    assert len(Timer.instances) == 1 and Timer.instances[0].delay == 3
    assert Timer.instances[0].cancelled and 'interrupt' not in adapter.calls


def test_sink_exception_remains_original_failure(clock):
    error = ValueError('OWNED REQUIRED FOLD REFUSAL')

    def sink(row):
        if row.kind == 'turn.completed':
            raise error

    result = invoke(Adapter(clock), sink)
    assert not result.success and result.reason == str(error)
    assert result.stdout == 'OWNED ADMITTED ANSWER'


def test_prestart_cancel_never_starts_provider(clock):
    adapter = Adapter(clock)
    result = invoke(adapter, cancelled=lambda: True)
    assert not result.success and result.reason == 'cancelled'
    assert 'start' not in adapter.calls and 'consume' not in adapter.calls and adapter.closed


@pytest.mark.parametrize('kind', ['message.delta', 'message.completed', 'runtime.warning', 'runtime.error'])
def test_real_controller_receipt_filters_duplicate_raw_content(clock, kind):
    from dataclasses import replace
    from test_tui_fold_admission import App, rows
    app = App()
    events = rows()
    refused = replace(events[2], seq=4, kind=kind, payload={
        'text': 'OWNED DUPLICATE RAW BODY', 'item_id': 'refused'})
    events.insert(-1, refused)
    events[-1] = replace(events[-1], seq=5)
    try:
        result = invoke(Adapter(clock, events=events), sink=app._ingest_runtime_event)
        assert result.success and result.stdout == 'OWNED CANONICAL ANSWER'
        assert not result.stderr and app._event_controller.snapshot().assistant_text == result.stdout
        assert not app._event_controller.snapshot().error_text
    finally:
        app._event_controller.close()


def test_presentation_failure_is_not_a_required_sink_refusal(clock, monkeypatch):
    from test_tui_fold_admission import App, rows
    app = App()
    app._ui_thread_id = -1
    monkeypatch.setattr(app, 'call_from_thread', lambda *_args: (_ for _ in ()).throw(
        RuntimeError('OWNED PRESENTATION UNAVAILABLE')), raising=False)
    try:
        result = invoke(Adapter(clock, events=rows()), sink=app._ingest_runtime_event)
        assert result.success and result.stdout == 'OWNED CANONICAL ANSWER'
        assert app._event_controller.snapshot().turn_state == 'SUCCEEDED'
    finally:
        app._event_controller.close()


def test_saved_provider_terminal_retained_when_journal_spends_original_deadline(clock, monkeypatch, tmp_path):
    from dataclasses import replace
    from pbatch.tui_controller import TUIEventController
    from pbatch.tui_journal import RuntimeEventJournal
    from test_tui_fold_admission import App, rows
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setenv('PBATCH_DATA_DIR', str(tmp_path / 'runtime'))
    app = App()
    app._event_controller.close()
    journal = RuntimeEventJournal(str(workspace), 'owned-session')
    app._event_controller = TUIEventController(journal=journal)
    original = journal.append

    def append(row):
        value = original(row)
        if row.kind == 'turn.completed':
            clock.now = 103.0
        return value

    monkeypatch.setattr(journal, 'append', append)
    try:
        events = [replace(row, workspace_id=str(workspace)) for row in rows()]
        result = invoke(Adapter(clock, events=events), sink=app._ingest_runtime_event)
        assert not result.success and result.reason == 'agent timed out after 3s'
        assert app._event_controller.snapshot().turn_state == 'SUCCEEDED'
        saved = journal.recover().events
        assert saved[-1].kind == 'turn.completed'
        assert not any(row.kind == 'turn.failed' for row in saved)
    finally:
        app._event_controller.close()
