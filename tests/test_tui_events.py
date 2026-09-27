"""Runtime EventBus and rebuildable Projection tests."""

from __future__ import annotations

import queue
import threading

import pytest

from pbatch.tui_events import EventBus, MAX_USAGE_KEYS, Projection
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


def test_sequence_preview_neither_reserves_nor_delivers_events():
    bus = EventBus()
    future = _event(seq=10)
    assert bus.validate_sequence(future)
    assert bus.validate_sequence(future)
    with pytest.raises(queue.Empty):
        bus.get(timeout=0)
    first = _event(seq=1)
    assert bus.publish(first)
    assert not bus.validate_sequence(first)
    with pytest.raises(ValueError, match="non-monotonic"):
        bus.validate_sequence(_event(seq=1, event_id="evt_backwards"))
    assert bus.publish(_event(seq=2))


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


def test_event_bus_fanout_backpressure_is_atomic():
    bus = EventBus(max_events=1)
    subscriber = bus.subscribe()
    first = _event(1)
    second = _event(2, event_id="evt_second")
    assert bus.publish(first)
    assert bus.get(timeout=0.1) == first

    with pytest.raises(OverflowError, match="bus is full"):
        bus.publish(second)
    with pytest.raises(queue.Empty):
        bus.get(timeout=0.01)
    assert subscriber.get(timeout=0.1) == first

    assert bus.publish(second)
    assert bus.get(timeout=0.1) == second
    assert subscriber.get(timeout=0.1) == second


def test_event_bus_delivers_canonical_independent_copies():
    bus = EventBus()
    first = bus.subscribe()
    second = bus.subscribe()
    event = _event(payload={"nested": {"value": "original"}})
    assert bus.publish(event)
    event.payload["nested"]["value"] = "producer mutation"

    primary = bus.get(timeout=0.1)
    one = first.get(timeout=0.1)
    two = second.get(timeout=0.1)
    assert primary.payload["nested"]["value"] == "original"
    primary.payload["nested"]["value"] = "consumer mutation"
    assert one.payload["nested"]["value"] == "original"
    assert two.payload["nested"]["value"] == "original"


def test_event_bus_close_is_idempotent_and_wakes_every_consumer():
    bus = EventBus()
    subscriber = bus.subscribe()
    started = threading.Barrier(3)
    failures = []

    def consume(getter):
        started.wait()
        try:
            getter()
        except Exception as exc:  # exact failures are asserted below
            failures.append(exc)

    threads = [
        threading.Thread(target=consume, args=(bus.get,)),
        threading.Thread(target=consume, args=(subscriber.get,)),
    ]
    for thread in threads:
        thread.start()
    started.wait()
    bus.close()
    bus.close()
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert len(failures) == 2 and all(isinstance(exc, RuntimeError) for exc in failures)
    assert bus.subscriber_count == 0
    with pytest.raises(RuntimeError, match="closed"):
        bus.publish(_event())
    with pytest.raises(RuntimeError, match="closed"):
        bus.subscribe()


def test_event_bus_indices_stay_bounded_and_run_churn_fails_closed():
    bus = EventBus(max_events=2, index_limit=37)
    for index in range(1, 10_001):
        event = _event(index, event_id=f"evt_capacity_{index}")
        assert bus.publish(event)
        assert bus.get(timeout=0.1).seq == index
    assert len(bus._event_ids) <= 37
    assert len(bus._last_seq_by_run) <= 37

    for index in range(36):
        event = _event(1, event_id=f"evt_run_{index}", run_id=f"run_{index}")
        assert bus.publish(event)
        bus.get(timeout=0.1)
    with pytest.raises(OverflowError, match="sequence index is full"):
        bus.publish(_event(
            1, event_id="evt_run_overflow", run_id="run_overflow"))
    assert len(bus._event_ids) <= 37
    assert len(bus._last_seq_by_run) <= 37


def test_event_bus_never_forgets_sequence_for_a_retained_run():
    bus = EventBus(max_events=1, index_limit=1)
    assert bus.publish(_event(10, run_id="run_a"))
    bus.get(timeout=0)
    with pytest.raises(OverflowError, match="sequence index is full"):
        bus.publish(_event(1, event_id="evt_run_b", run_id="run_b"))
    with pytest.raises(ValueError, match="non-monotonic"):
        bus.publish(_event(5, event_id="evt_run_a_late", run_id="run_a"))


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
        _event(1, "turn.started"),
        _event(2, "command.started"),
        _event(3, "command.completed"),
        _event(4, "tool.started"),
        _event(5, "tool.failed"),
        _event(6, "validation.started"),
        _event(7, "validation.completed"),
        _event(8, "file.changed"),
        _event(9, "approval.requested"),
        _event(10, "approval.resolved"),
        _event(11, "usage.updated", payload={
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


def test_projection_bounds_dynamic_usage_keys_and_still_updates_known_keys():
    projection = Projection()
    usage = {f"metric_{index}": index for index in range(MAX_USAGE_KEYS + 20)}
    assert projection.register_event(_event(
        1, "usage.updated", turn_id="", payload={"usage": usage}))

    assert len(projection.stats.usage) == MAX_USAGE_KEYS
    assert len(projection.stats_by_run["run_one"].usage) == MAX_USAGE_KEYS
    assert projection.register_event(_event(
        2, "usage.updated", turn_id="", payload={
            "usage": {"metric_0": 5, "overflow_metric": 9},
        }))
    assert projection.stats.usage["metric_0"] == 5.0
    assert projection.stats_by_run["run_one"].usage["metric_0"] == 5.0
    assert "overflow_metric" not in projection.stats.usage
    assert "overflow_metric" not in projection.stats_by_run["run_one"].usage


def test_projection_recent_events_are_a_bounded_ring():
    projection = Projection(recent_limit=2)
    assert projection.register_event(_event(1, "turn.started"))
    events = [_event(index, "message.delta") for index in (2, 3, 4)]
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
    assert projection.register_event(_event(1, "turn.started"))
    event = _event(2, "command.completed")
    assert projection.register_event(event) is True
    assert projection.register_event(event) is False
    assert projection.stats["command"] == 1
    assert projection.duplicate_count == 1


def test_projection_uses_legal_turn_transitions_and_allows_steer_noop():
    projection = Projection()
    assert projection.register_event(_event(1, "turn.started"))
    assert projection.register_event(_event(2, "turn.steered"))
    assert projection.turns["turn_one"].state == TurnState.RUNNING

    with pytest.raises(ValueError, match="illegal turn transition"):
        projection.register_event(_event(
            3, "turn.started", event_id="evt_restarted"))
    assert projection.turns["turn_one"].state == TurnState.RUNNING
    assert "evt_restarted" not in projection._event_ids


def test_projection_rejects_steer_before_turn_is_running():
    projection = Projection()
    assert projection.register_event(_event(1, "turn.queued"))
    with pytest.raises(ValueError, match="requires a running turn"):
        projection.register_event(_event(2, "turn.steered"))
    assert projection.turns["turn_one"].state == TurnState.QUEUED


def test_projection_failed_event_can_project_explicit_unknown_state():
    projection = Projection()
    assert projection.register_event(_event(1, "turn.started"))
    assert projection.register_event(_event(
        2, "turn.failed", payload={"state": TurnState.UNKNOWN}))
    assert projection.turns["turn_one"].state == TurnState.UNKNOWN
    assert projection.sessions["ses_one"].terminal_state == TurnState.UNKNOWN


@pytest.mark.parametrize(("kind", "payload_state", "expected"), [
    ("turn.completed", TurnState.FAILED, TurnState.SUCCEEDED),
    ("turn.completed", TurnState.UNKNOWN, TurnState.SUCCEEDED),
    ("turn.failed", TurnState.SUCCEEDED, TurnState.FAILED),
])
def test_projection_terminal_event_kind_cannot_be_spoofed_by_payload(
        kind, payload_state, expected):
    projection = Projection()
    assert projection.register_event(_event(1, "turn.started"))
    assert projection.register_event(_event(
        2, kind, payload={"state": payload_state}))
    assert projection.turns["turn_one"].state == expected


@pytest.mark.parametrize("kind", [
    "turn.completed", "turn.failed", "turn.interrupting", "turn.interrupted",
])
def test_projection_rejects_terminal_or_interrupt_as_first_turn_event(kind):
    projection = Projection()
    with pytest.raises(ValueError, match="turn must begin"):
        projection.register_event(_event(1, kind))
    assert not projection.turns and not projection.recent_events


def test_projection_rejects_interrupting_to_running_regression():
    projection = Projection()
    assert projection.register_event(_event(1, "turn.started"))
    assert projection.register_event(_event(2, "turn.interrupting"))
    with pytest.raises(ValueError, match="illegal turn transition"):
        projection.register_event(_event(
            3, "turn.started", event_id="evt_illegal_restart"))
    assert projection.turns["turn_one"].state == TurnState.INTERRUPTING


@pytest.mark.parametrize(("field", "value"), [
    ("workspace_id", "ws_other"),
    ("session_id", "ses_other"),
    ("run_id", "run_other"),
    ("actor_id", "agent_other"),
])
def test_projection_rejects_existing_turn_binding_mismatch(field, value):
    projection = Projection()
    assert projection.register_event(_event(1, "turn.started"))
    mismatch = _event(2, "message.delta", event_id=f"evt_bad_{field}")
    setattr(mismatch, field, value)

    with pytest.raises(ValueError, match="binding mismatch"):
        projection.register_event(mismatch)
    assert list(projection.sessions) == ["ses_one"]
    assert list(projection.recent_events) == [_event(1, "turn.started")]


def test_projection_rejects_session_workspace_mismatch_for_a_new_turn():
    projection = Projection()
    assert projection.register_event(_event(1, "turn.started"))
    with pytest.raises(ValueError, match="session workspace binding mismatch"):
        projection.register_event(_event(
            1, "turn.started", event_id="evt_new_turn_wrong_workspace",
            turn_id="turn_two", run_id="run_two", workspace_id="ws_other"))
    assert list(projection.turns) == ["turn_one"]


def test_terminal_turn_late_events_only_enter_bounded_recent_except_workspace():
    projection = Projection(recent_limit=3)
    assert projection.register_event(_event(1, "turn.started"))
    assert projection.register_event(_event(2, "turn.completed"))
    terminal_time = projection.sessions["ses_one"].last_event_at
    late = [
        _event(3, "message.delta", event_id="evt_late_message"),
        _event(4, "command.completed", event_id="evt_late_command"),
        _event(5, "file.changed", event_id="evt_late_file"),
        _event(6, "turn.steered", event_id="evt_late_steer_two"),
    ]
    for event in late:
        assert projection.register_event(event) is False

    assert projection.turns["turn_one"].state == TurnState.SUCCEEDED
    assert projection.sessions["ses_one"].last_event_at == terminal_time
    assert projection.stats["command"] == 0 and projection.stats["file"] == 0
    assert [event.event_id for event in projection.recent_events] == [
        "evt_late_command", "evt_late_file", "evt_late_steer_two"]
    workspace = _event(7, "workspace.snapshot", event_id="evt_after_workspace")
    assert projection.register_event(workspace) is True
    assert projection.sessions["ses_one"].last_event_at == workspace.occurred_at
    assert projection.turns["turn_one"].state == TurnState.SUCCEEDED


def test_projection_defensively_copies_registered_event_payload():
    projection = Projection()
    event = _event(
        1, "usage.updated", turn_id="",
        payload={"usage": {"input_tokens": 3}})
    assert projection.register_event(event)
    event.payload["usage"]["input_tokens"] = 999
    assert projection.recent_events[-1].payload["usage"]["input_tokens"] == 3
    assert projection.stats.usage["input_tokens"] == 3


def test_projection_entity_maps_evict_terminal_state_and_stay_bounded():
    projection = Projection(recent_limit=5, entity_limit=3)
    for index in range(20):
        values = {
            "workspace_id": f"ws_{index}", "session_id": f"ses_{index}",
            "run_id": f"run_{index}", "turn_id": f"turn_{index}",
            "actor_id": f"actor_{index}",
        }
        assert projection.register_event(_event(
            1, "turn.started", event_id=f"evt_start_{index}", **values))
        assert projection.register_event(_event(
            2, "turn.completed", event_id=f"evt_done_{index}", **values))

    assert 0 < len(projection.sessions) <= 3
    assert 0 < len(projection.turns) <= 3
    assert 0 < len(projection.current_turns) <= 3
    assert 0 < len(projection.stats_by_run) <= 3
    assert 0 < len(projection._event_ids) <= 3
    assert {turn.turn_id for turn in projection.current_turns.values()} <= set(
        projection.turns)


def test_projection_drops_late_events_for_an_evicted_terminal_turn():
    projection = Projection(entity_limit=1)
    first = {
        "workspace_id": "ws_first", "session_id": "ses_first",
        "run_id": "run_first", "turn_id": "turn_first",
    }
    second = {
        "workspace_id": "ws_second", "session_id": "ses_second",
        "run_id": "run_second", "turn_id": "turn_second",
    }
    assert projection.register_event(_event(
        1, "turn.started", event_id="evt_first_start", **first))
    assert projection.register_event(_event(
        2, "turn.completed", event_id="evt_first_done", **first))
    assert projection.register_event(_event(
        1, "turn.started", event_id="evt_second_start", **second))
    assert "turn_first" in projection._retired_turn_ids

    late = _event(
        3, "message.delta", event_id="evt_first_late",
        payload={"text": "must not return"}, **first)
    assert projection.register_event(late) is False
    assert "turn_first" not in projection.turns
    assert projection.drop_count == 1


def test_projection_rejects_unknown_turn_scoped_event():
    projection = Projection()
    with pytest.raises(ValueError, match="requires a known turn"):
        projection.register_event(_event(1, "message.delta"))


def test_projection_rejects_overlapping_turns_for_one_run():
    projection = Projection()
    assert projection.register_event(_event(1, "turn.started"))
    with pytest.raises(ValueError, match="already has an active turn"):
        projection.register_event(_event(
            2, "turn.started", event_id="evt_overlap",
            turn_id="turn_two"))


def test_projection_overflow_is_explicit_when_no_terminal_entity_is_safe():
    projection = Projection(entity_limit=1)
    assert projection.register_event(_event(1, "turn.started"))
    other = _event(
        1, "turn.started", event_id="evt_other_active", workspace_id="ws_two",
        session_id="ses_two", run_id="run_two", turn_id="turn_two")
    with pytest.raises(OverflowError, match="session limit"):
        projection.register_event(other)

    assert list(projection.sessions) == ["ses_one"]
    assert list(projection.turns) == ["turn_one"]
    assert list(projection.stats_by_run) == ["run_one"]
    assert "evt_other_active" not in projection._event_ids


def test_projection_run_stats_overflow_keeps_active_run_unchanged():
    projection = Projection(entity_limit=1)
    assert projection.register_event(_event(1, "turn.started"))
    warning = _event(
        1, "runtime.warning", event_id="evt_other_run", run_id="run_two",
        turn_id="", payload={"text": "warning"})
    with pytest.raises(OverflowError, match="run limit"):
        projection.register_event(warning)
    assert list(projection.stats_by_run) == ["run_one"]
    assert "evt_other_run" not in projection._event_ids


@pytest.mark.parametrize(("name", "value"), [
    ("recent_limit", 0), ("recent_limit", True),
    ("entity_limit", 0), ("entity_limit", True),
])
def test_projection_rejects_invalid_limits(name, value):
    kwargs = {name: value}
    with pytest.raises(ValueError, match=f"{name} must be a positive integer"):
        Projection(**kwargs)
