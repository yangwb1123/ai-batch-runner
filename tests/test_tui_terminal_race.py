"""A timed-out queue read must not discard an already-published terminal."""

import queue
import threading
from types import SimpleNamespace

import pytest

from pbatch.tui import PiBatchTUI
from pbatch.tui_commands import TUIReviewMixin
from pbatch.tui_controller import TUIEventController
from pbatch.tui_lifecycle import (
    RuntimeOperation, TUIRuntimeLifecycleMixin, next_runtime_event,
)
from pbatch.tui_protocol import RuntimeEvent, TurnState
from pbatch.tui_review import AppServerReviewCapture
from pbatch.tui_runtime import TurnHandle


class _PublishAfterEmpty(queue.Queue):
    """Schedule a producer between the Empty exception and the done check."""

    def __init__(self, handle):
        super().__init__()
        self.handle = handle
        self.timeouts = []

    def get(self, *, timeout):
        self.timeouts.append(timeout)
        if len(self.timeouts) == 1:
            try:
                return super().get(block=False)
            except queue.Empty:
                for seq, kind, state in (
                        (1, "turn.started", TurnState.RUNNING),
                        (2, "turn.completed", TurnState.SUCCEEDED)):
                    self.put(RuntimeEvent(
                        event_id=f"evt_terminal_race_{seq}", seq=seq,
                        occurred_at="2026-09-22T12:00:00Z", workspace_id="workspace",
                        session_id=self.handle.session_id, run_id=self.handle.run_id,
                        turn_id=self.handle.turn_id, actor_id=self.handle.actor_id,
                        kind=kind, payload={"state": state}))
                self.handle.state = TurnState.SUCCEEDED
                self.handle._done.set()
                raise
        return super().get(timeout=timeout)


@pytest.mark.parametrize("consumer", ["legacy", "managed", "review"])
def test_terminal_published_after_empty_reaches_projection(consumer):
    handle = TurnHandle("session", "run", "turn", "agent_main", "now")
    stream = _PublishAfterEmpty(handle)
    controller = TUIEventController()
    closed = threading.Event()
    operation = (None if consumer == "legacy" else RuntimeOperation(
        SimpleNamespace(close=closed.set), 5, "test"))
    app = SimpleNamespace(
        _runtime_event_context=PiBatchTUI._runtime_event_context,
        _ingest_runtime_event=controller.ingest, _runtime_operation=operation)
    try:
        if consumer == "review":
            kind, _ = TUIReviewMixin._consume_appserver_review(
                app, stream, handle, AppServerReviewCapture())
            assert kind == "turn.completed"
        else:
            result = TUIRuntimeLifecycleMixin._consume_adapter_turn(
                app, stream, handle, operation)
            assert result["success"]
        assert controller.snapshot().turn_state == TurnState.SUCCEEDED
        assert stream.timeouts == [0.1, 0, 0.1]
        assert not closed.is_set()
    finally:
        if operation is not None:
            operation.finish()
        controller.close()


def test_done_without_a_terminal_still_fails_closed():
    handle = TurnHandle("session", "run", "turn", "agent_main", "now")
    handle._done.set()
    with pytest.raises(RuntimeError, match="ended without a terminal event"):
        next_runtime_event(queue.Queue(), handle)


def test_pending_handle_keeps_empty_poll_retryable():
    handle = TurnHandle("session", "run", "turn", "agent_main", "now")
    with pytest.raises(queue.Empty):
        next_runtime_event(queue.Queue(), handle)
