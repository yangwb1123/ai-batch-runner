"""Runtime EventBus and rebuildable Projection tests."""

from __future__ import annotations

import queue

import pytest

from pbatch.tui_events import EventBus, Projection
from pbatch.tui_protocol import RuntimeEvent, TurnState


def _event(seq=1, kind="turn.started", **changes):
    values = {
        "event_id": f"evt_{seq}_{kind.replace('.', '_')}",
        "seq": seq,
        "occurred_at": f"2026-08-21T12:00:{seq:02d}Z",
        "workspace_id": "ws_one",
        "session_id": "ses_one",
        "run_id": "run_one",
        "turn_id": "turn_one",
        "actor_id": "agent_main",
        "kind": kind,
        "payload": {},
    }
    values.update(changes)
    return RuntimeEvent(**values)


def test_event_bus_deduplicates_event_id_before_delivery():
    bus = EventBus()
    event = _event()
    assert bus.publish(event) is True
    assert bus.publish(event) is False
    assert bus.get(timeout=0.1) == event
    with pytest.raises(queue.Empty):
        bus.get(timeout=0.01)


def test_event_bus_rejects_non_monotonic_sequence_per_run():
    bus = EventBus()
    assert bus.publish(_event(seq=3)) is True
    with pytest.raises(ValueError, match="non-monotonic seq"):
        bus.publish(_event(seq=2, event_id="evt_late"))


def test_event_bus_assigns_zero_sequence_monotonically():
    bus = EventBus()
    first = _event(seq=0, event_id="evt_auto_one")
    second = _event(seq=0, event_id="evt_auto_two")
    assert bus.publish(first) is True
    assert bus.publish(second) is True
    assert (first.seq, second.seq) == (1, 2)


def test_event_bus_overflow_rejects_new_event_without_silent_drop():
    bus = EventBus(max_events=1)
    first = _event(seq=1)
    second = _event(seq=2)
    assert bus.publish(first) is True
    with pytest.raises(OverflowError, match="bus is full"):
        bus.publish(second)
    assert bus.get(timeout=0.1) == first
    assert bus.publish(second) is True
    assert bus.get(timeout=0.1) == second


def test_subscribers_consume_independently_and_are_strongly_registered():
    bus = EventBus()
    first = bus.subscribe()
    second = bus.subscribe()
    event = _event()
    assert bus.subscriber_count == 2
    bus.publish(event)
    assert first.get(timeout=0.1) == event
    assert second.get(timeout=0.1) == event
    assert bus.get(timeout=0.1) == event
    assert bus.unsubscribe(first) is True
    assert bus.unsubscribe(first) is False
    assert bus.subscriber_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        first.get(timeout=0.1)


def test_projection_ignores_late_turn_event_after_terminal_state():
    projection = Projection()
    assert projection.register_event(_event(seq=1, kind="turn.started"))
    assert projection.register_event(_event(seq=2, kind="turn.completed"))
    late = _event(seq=3, kind="turn.steered", event_id="evt_late_steer")
    assert projection.register_event(late) is False
    assert projection.turns["turn_one"].state == TurnState.SUCCEEDED
    assert projection.sessions["ses_one"].terminal_state == TurnState.SUCCEEDED
    assert projection.sessions["ses_one"].turn_count == 1
    assert projection.drop_count == 1
    assert list(projection.recent_events)[-1] == late


def test_projection_counts_runtime_types_and_summarizes_usage():
    projection = Projection()
    events = [
        _event(1, "command.started"),
        _event(2, "command.completed"),
        _event(3, "tool.started"),
        _event(4, "tool.failed"),
        _event(5, "validation.started"),
        _event(6, "validation.completed"),
        _event(7, "file.changed"),
        _event(8, "approval.requested"),
        _event(9, "approval.resolved"),
        _event(10, "usage.updated", payload={
            "usage": {"input_tokens": 12, "output_tokens": 3, "cached": True},
        }),
    ]
    for event in events:
        assert projection.register_event(event)
    assert projection.stats.counts == {
        "command": 2, "tool": 2, "validation": 2, "file": 1,
        "approval": 2,
    }
    assert projection.stats.usage == {
        "input_tokens": 12.0, "output_tokens": 3.0,
    }
    assert projection.stats_by_run["run_one"].counts == projection.stats.counts


def test_projection_recent_events_are_a_bounded_ring():
    projection = Projection(recent_limit=2)
    events = [_event(index, "message.delta") for index in (1, 2, 3)]
    for event in events:
        projection.register_event(event)
    assert list(projection.recent_events) == events[-2:]


def test_projection_rebuild_matches_incremental_projection():
    events = [
        _event(1, "session.created", turn_id=""),
        _event(2, "turn.started"),
        _event(3, "command.completed"),
        _event(4, "file.changed"),
        _event(5, "usage.updated", payload={"input_tokens": 8}),
        _event(6, "turn.completed"),
        _event(7, "turn.interrupted", event_id="evt_late_terminal"),
    ]
    incremental = Projection(recent_limit=5)
    for event in events:
        incremental.register_event(event)
    rebuilt = Projection.rebuild(events, recent_limit=5)
    assert rebuilt.sessions == incremental.sessions
    assert rebuilt.turns == incremental.turns
    assert rebuilt.current_turns == incremental.current_turns
    assert rebuilt.stats == incremental.stats
    assert rebuilt.stats_by_run == incremental.stats_by_run
    assert list(rebuilt.recent_events) == list(incremental.recent_events)
    assert rebuilt.drop_count == incremental.drop_count == 1


def test_projection_deduplicates_replayed_event_ids():
    projection = Projection()
    event = _event(1, "command.completed")
    assert projection.register_event(event) is True
    assert projection.register_event(event) is False
    assert projection.stats["command"] == 1
    assert projection.duplicate_count == 1
