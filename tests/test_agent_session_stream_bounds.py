"""Streaming consumers apply bounded backpressure without relinquishing a turn."""

import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pbatch.agent_session import OwnedRuntimeCleanupFailed, create_agent_session
from test_agent_session import FakeRuntime, _options


def until(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def started_stream(session, runtime, *, timeout=None):
    stream = session.prompt_events("stream", timeout=timeout)
    first = []
    consumer = threading.Thread(target=lambda: first.append(next(stream)))
    consumer.start()
    assert runtime.turn_started.wait(1)
    runtime.emit(runtime.handles[0], "message.delta", {"text": "first"})
    consumer.join(1)
    assert not consumer.is_alive() and first[0].payload["text"] == "first"
    return stream, runtime.handles[0]


@pytest.mark.parametrize("count,text", [(300, "x"), (80, "x" * 32768)], ids=["count-limit", "byte-limit"])
def test_slow_stream_consumer_is_bounded_and_resumes_without_dropping_events(count, text):
    runtime = FakeRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        stream, handle = started_stream(session, runtime)
        received_last = threading.Event()
        session.subscribe(lambda event: received_last.set() if event.payload.get("last") else None)
        for index in range(count):
            runtime.emit(handle, "message.delta", {"text": text, "last": index == count - 1})
        try:
            assert not received_last.wait(0.2), "pump bypassed the bounded consumer window"
            with session._condition:
                tracker = session._active
                assert len(tracker.events) <= 256
                assert sum(len(event.to_jsonl().encode()) for event in tracker.events) <= 1024 * 1024
                assert not tracker.delta_text and not tracker.completed_text
            runtime.emit(handle, "turn.completed")
            remaining = list(stream)
            assert len(remaining) == count + 1
            assert remaining[-1].kind == "turn.completed" and received_last.is_set()
        finally:
            stream.close()


def test_closing_full_stream_wakes_pump_without_cancelling_or_releasing_turn():
    runtime = FakeRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        stream, handle = started_stream(session, runtime)
        for _ in range(300):
            runtime.emit(handle, "message.delta", {"text": "unread"})
        until(lambda: len(session._active.events) >= 256)
        stream.close()
        assert session.state["busy"] and session.state["active_turn_id"] == handle.turn_id
        with pytest.raises(RuntimeError, match="already processing"):
            session.prompt("must not replay", timeout=1)
        observed = threading.Event()
        session.subscribe(lambda event: observed.set() if event.kind == "turn.completed" else None)
        runtime.emit(handle, "turn.completed")
        assert observed.wait(1)
        assert not session.state["busy"]
        assert runtime.interrupts == [] and len(runtime.handles) == 1


def test_session_close_unblocks_a_full_stream_and_its_owned_pump():
    runtime = FakeRuntime()
    session = create_agent_session(_options(), runtime=runtime)
    stream, handle = started_stream(session, runtime)
    for _ in range(300):
        runtime.emit(handle, "message.delta", {"text": "unread"})
    until(lambda: len(session._active.events) >= 256)
    session.close()
    stream.close()
    assert runtime.closed and not session._event_thread.is_alive()


def test_consumer_exception_can_close_stream_while_runtime_keeps_ownership():
    runtime = FakeRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        stream, handle = started_stream(session, runtime)
        try:
            raise ValueError("consumer failed")
        except ValueError:
            stream.close()
        assert session.state["busy"] and len(runtime.handles) == 1
        runtime.emit(handle, "turn.failed", {"reason": "provider outcome"})
        until(lambda: not session.state["busy"])
        assert runtime.interrupts == []


def test_stream_deadline_does_not_extend_while_events_remain_buffered():
    runtime = FakeRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        stream, handle = started_stream(session, runtime, timeout=0.05)
        runtime.emit(handle, "message.delta", {"text": "buffered"})
        until(lambda: bool(session._active.events))
        time.sleep(0.06)
        with pytest.raises(TimeoutError, match="timed out"):
            next(stream)
        assert session.state["busy"] and runtime.interrupts == []
        runtime.emit(handle, "turn.completed")
        until(lambda: not session.state["busy"])


def test_stream_rechecks_deadline_after_condition_wakes_with_an_event(monkeypatch):
    from pbatch import agent_session

    clock = [100.0]
    monkeypatch.setattr(agent_session, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    runtime = FakeRuntime()
    with create_agent_session(_options(), runtime=runtime) as session:
        stream, handle = started_stream(session, runtime, timeout=1)
        wait = session._condition.wait

        def wake_after_deadline(remaining=None):
            runtime.emit(handle, "message.delta", {"text": "late wakeup"})
            result = wait(remaining)
            clock[0] = 102.0
            return result

        monkeypatch.setattr(session._condition, "wait", wake_after_deadline)
        try:
            with pytest.raises(TimeoutError, match="timed out"):
                next(stream)
            assert session.state["busy"] and runtime.interrupts == []
        finally:
            stream.close()


class InvalidHandleRuntime(FakeRuntime):
    def __init__(self, field, value, *, close_fails=False):
        super().__init__()
        self.field, self.value = field, value
        self.close_fails = close_fails
        self.close_calls = 0

    def start_turn(self, request):
        handle = super().start_turn(request)
        self.emit(handle, "turn.completed")
        return replace(handle, **{self.field: self.value})

    def close(self):
        self.close_calls += 1
        if self.close_fails:
            raise OSError("cleanup unavailable")
        super().close()


@pytest.mark.parametrize("field,value", [("session_id", "foreign"), ("run_id", ""), ("turn_id", ""), ("actor_id", None)])
def test_invalid_start_handle_closes_owned_runtime_and_never_replays(field, value):
    runtime = InvalidHandleRuntime(field, value)
    session = create_agent_session(_options(), runtime=runtime)
    try:
        with pytest.raises(TypeError, match="turn identity"):
            list(session.prompt_events("first", timeout=0.05))
        session.close()
        assert runtime.closed and runtime.close_calls == 1
        assert session.state["closed"] and not session._event_thread.is_alive()
        with pytest.raises(RuntimeError, match="closed"):
            session.prompt("must not replay", timeout=0.05)
        assert len(runtime.handles) == 1
    finally:
        session.close()


def test_failed_cleanup_after_invalid_handle_is_reported_without_claiming_stop():
    runtime = InvalidHandleRuntime("session_id", "foreign", close_fails=True)
    session = create_agent_session(_options(), runtime=runtime)
    try:
        with pytest.raises(OwnedRuntimeCleanupFailed, match="cleanup failed.*outcome unknown") as first:
            list(session.prompt_events("first", timeout=0.05))
        assert not runtime.closed and runtime.close_calls == 1
        assert session.state["closed"]
        assert "cleanup failed" in str(session._stream_error)
        with pytest.raises(RuntimeError, match="closed"):
            session.prompt("must not replay", timeout=0.05)
        for _ in range(2):
            with pytest.raises(OwnedRuntimeCleanupFailed) as repeated:
                session.close()
            assert repeated.value is first.value
        assert runtime.close_calls == 1 and not runtime.closed
        with pytest.raises(RuntimeError, match="closed"):
            session.prompt("still must not replay", timeout=0.05)
        assert len(runtime.handles) == 1
    finally:
        runtime.close_fails = False
        runtime.close()
        session._event_thread.join(1)
