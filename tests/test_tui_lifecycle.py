"""Bounded direct-runtime turns, review cancellation and startup races."""

import asyncio
import queue
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from textual.widgets import Input, Markdown

from pbatch import config
from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_backend import TUIBackend
from pbatch.tui_lifecycle import (
    RuntimeOperation, RuntimeOperationStopped, TUIRuntimeLifecycleMixin)
from pbatch.tui_protocol import RuntimeCapabilities, RuntimeEvent, TurnState
from pbatch.tui_runtime import RuntimeInfo, RuntimeSession, TurnHandle


class _Runtime:
    def __init__(self, cwd, *, mode="app-server", start_blocked=False,
                 noisy=False, success=False):
        self.cwd, self.mode = cwd, mode
        self.start_blocked, self.noisy, self.success = start_blocked, noisy, success
        self.entered, self.closed = threading.Event(), threading.Event()
        self.interrupts, self.calls = [], []
        self.queue = queue.Queue()
        self.handle, self.seq = None, 0
        self.session = RuntimeSession(
            uuid.uuid4().hex, "provider", cwd, "now", "ACTIVE")

    def runtime_info(self):
        name = "pi-rpc" if self.mode == "pi-rpc" else "codex-app-server"
        return RuntimeInfo(name, 1, RuntimeCapabilities(
            runtime=name, capabilities={"review": True, "interrupt": True}))

    def initialize(self, request):
        return self.runtime_info()

    def start_session(self, request):
        return self.session

    def start_turn(self, request):
        self.calls.append(request)
        self.entered.set()
        if self.start_blocked:
            assert self.closed.wait(4), "deadline never closed a blocked start RPC"
            raise RuntimeError("transport closed during start")
        self.handle = TurnHandle(
            self.session.session_id, request.get("run_id", "run"),
            request.get("turn_id", "turn"), "agent_main", "now")
        self.queue.put(self.event("turn.started", {"state": TurnState.RUNNING}))
        if self.success:
            self.queue.put(self.event("turn.completed", {"state": TurnState.SUCCEEDED}))
            self.handle.state = TurnState.SUCCEEDED
            self.handle._done.set()
        return self.handle

    start_review = start_turn

    def event(self, kind, payload):
        self.seq += 1
        return RuntimeEvent(
            event_id=f"evt_lifecycle_{self.seq}", seq=self.seq,
            occurred_at="2026-09-22T00:00:00Z",
            workspace_id=self.cwd, session_id=self.session.session_id,
            run_id=self.handle.run_id, turn_id=self.handle.turn_id,
            actor_id=self.handle.actor_id, kind=kind, payload=payload)

    def events(self):
        return self

    def get(self, timeout=None):
        if self.noisy and timeout != 0 and self.handle and self.queue.empty():
            time.sleep(0.005)
            return self.event("usage.updated", {"usage": {"input_tokens": self.seq}})
        return self.queue.get(timeout=timeout)

    def interrupt_turn(self, request):
        self.interrupts.append(request)
        assert request == {"turn_id": self.handle.turn_id}
        self.closed.wait(2)  # model a provider that never acknowledges interrupt

    def close(self):
        self.closed.set()
        self.queue.put(None)


def _app(tmp_path, runtime, *, timeout=0.12, factory=None):
    factory = factory or (lambda *args, **kwargs: runtime)
    return PiBatchTUI(
        TuiOptions(cwd=str(tmp_path), agent="pi", runtime=runtime.mode,
                   timeout=timeout, memory_mode="off"),
        backend=TUIBackend(cwd=str(tmp_path), agent="pi", timeout=30, memory_mode="off"),
        appserver_factory=factory, pi_rpc_factory=factory)


async def _until(predicate, pilot):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        await pilot.pause(0.01)
    assert predicate()


@pytest.mark.parametrize("mode", ["pi-rpc", "app-server"])
@pytest.mark.parametrize("noisy", [False, True])
def test_direct_turn_deadline_stops_silent_and_busy_event_streams(tmp_path, mode, noisy):
    async def scenario():
        runtime = _Runtime(str(tmp_path), mode=mode, noisy=noisy)
        app = _app(tmp_path, runtime)
        async with app.run_test() as pilot:
            assert app._start_turn("bounded")
            await _until(lambda: not app._busy, pilot)
            assert runtime.closed.is_set()
            assert runtime.interrupts
            assert "timed out" in app.query_one("#conversation", Markdown).source
            assert app._runtime_view.turn_state != TurnState.SUCCEEDED
            assert not app.query_one("#composer", Input).disabled
            assert not app._start_turn("cannot reuse an uncertain session")
    asyncio.run(scenario())


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_start_rpc_is_covered_by_deadline_and_cancel(tmp_path, review, cancel):
    async def scenario():
        runtime = _Runtime(str(tmp_path), start_blocked=True)
        app = _app(tmp_path, runtime, timeout=3 if cancel else 0.1)
        async with app.run_test() as pilot:
            if review:
                app._slash_review(())
            else:
                app._start_turn("blocked start")
            await _until(runtime.entered.is_set, pilot)
            if cancel:
                app.action_cancel()
            await _until(lambda: not app._busy, pilot)
            assert runtime.closed.is_set()
            assert not runtime.interrupts  # no published handle to authorize stop
            expected = "cancelled" if cancel else "timed out"
            assert expected in app.query_one("#conversation", Markdown).source
            assert not app._review_busy
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_appserver_review_stops_and_restores_composer(tmp_path, cancel):
    async def scenario():
        runtime = _Runtime(str(tmp_path))
        app = _app(tmp_path, runtime, timeout=3 if cancel else 0.1)
        async with app.run_test() as pilot:
            app._slash_review(())
            await _until(lambda: app._runtime_view.turn_state == TurnState.RUNNING, pilot)
            if cancel:
                app.action_cancel()
            await _until(lambda: not app._busy, pilot)
            assert runtime.closed.is_set() and runtime.interrupts
            assert app._runtime_view.turn_state != TurnState.SUCCEEDED
            assert not app.query_one("#composer", Input).disabled
    asyncio.run(scenario())


def test_new_session_reconnects_after_deadline(tmp_path):
    async def scenario():
        runtimes = [_Runtime(str(tmp_path)), _Runtime(str(tmp_path), success=True)]
        available = iter(runtimes)
        app = _app(tmp_path, runtimes[0], factory=lambda *_a, **_kw: next(available))
        async with app.run_test() as pilot:
            app._start_turn("will expire")
            await _until(lambda: not app._busy, pilot)
            app._new_session()
            assert not app._runtime_blocked_reason
            assert app._adapter is runtimes[1]
            app.options.timeout = 2
            assert app._start_turn("fresh operation")
            await _until(lambda: not app._busy, pilot)
            assert app._runtime_view.turn_state == TurnState.SUCCEEDED
            assert not runtimes[1].closed.is_set()
    asyncio.run(scenario())


def test_zero_option_uses_configured_default_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_DEFAULT_TIMEOUT", 0.1)
    async def scenario():
        runtime = _Runtime(str(tmp_path))
        app = _app(tmp_path, runtime, timeout=0)
        async with app.run_test() as pilot:
            app._start_turn("default deadline")
            await _until(lambda: not app._busy, pilot)
            assert "timed out after 0.1s" in app._runtime_blocked_reason
    asyncio.run(scenario())


def test_completion_disarms_watchdog_and_cancel_before_bind_is_latched():
    runtime = SimpleNamespace(close=threading.Event().set)
    operation = RuntimeOperation(runtime, 0.02, "test")
    operation.complete()
    time.sleep(0.04)
    assert not operation.reason
    assert not operation.cancel()
    operation = RuntimeOperation(runtime, 10, "test")
    assert operation.cancel()
    with pytest.raises(RuntimeOperationStopped, match="cancelled"):
        operation.bind(SimpleNamespace(done=False))
    operation.finish()
    assert operation.closed


def test_rejected_terminal_projection_still_closes_runtime(tmp_path):
    runtime = _Runtime(str(tmp_path), success=True)
    handle = runtime.start_turn({})
    operation = RuntimeOperation(runtime, 5, "turn")
    operation.bind(handle)
    def ingest(event):
        if event.kind == "turn.completed":
            raise ValueError("terminal journal write rejected")
    app = SimpleNamespace(
        _runtime_event_context=lambda _handle, event: (event.kind, event.payload),
        _ingest_runtime_event=ingest, _runtime_operation=operation)
    with pytest.raises(ValueError, match="journal write") as caught:
        TUIRuntimeLifecycleMixin._consume_adapter_turn(app, runtime, handle, operation)
    error = TUIRuntimeLifecycleMixin._finish_runtime_operation(app, operation, caught.value)
    assert isinstance(error, RuntimeOperationStopped)
    assert runtime.closed.is_set()
    assert "journal write rejected" in app._runtime_blocked_reason


def test_quit_while_appserver_review_start_is_waiting(tmp_path):
    async def scenario():
        runtime = _Runtime(str(tmp_path), start_blocked=True)
        app = _app(tmp_path, runtime, timeout=30)
        async with app.run_test() as pilot:
            app._slash_review(())
            await _until(runtime.entered.is_set, pilot)
            app.action_quit()
        assert runtime.closed.is_set()
        assert not runtime.interrupts
    asyncio.run(scenario())


def test_review_cancel_before_worker_start_never_calls_provider(tmp_path, monkeypatch):
    async def scenario():
        runtime = _Runtime(str(tmp_path))
        app = _app(tmp_path, runtime, timeout=30)
        async with app.run_test() as pilot:
            pending = []
            monkeypatch.setattr(app, "run_worker", lambda target, **_kw: pending.append(target))
            app._slash_review(())
            app.action_cancel()
            assert pending
            await asyncio.to_thread(pending[0])
            assert not runtime.calls
            assert not app._busy and not app._review_busy
            assert runtime.closed.is_set()
    asyncio.run(scenario())
