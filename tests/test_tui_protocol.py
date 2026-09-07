"""TUI Runtime 纯数据协议的确定性契约测试。"""

from __future__ import annotations

import json

import pytest

from pbatch.tui_protocol import (
    MAX_EVENT_EXTENSIONS_BYTES,
    MAX_EVENT_LINE_BYTES,
    MAX_EVENT_PAYLOAD_BYTES,
    RUNTIME_EVENT_KINDS,
    TERMINAL_TURN_STATES,
    TURN_STATES,
    TURN_TRANSITIONS,
    EventSequenceTracker,
    RuntimeCapabilities,
    RuntimeEvent,
    TurnState,
    is_duplicate,
    turn_transition,
)


EXPECTED_EVENT_KINDS = frozenset("""
session.created session.resumed session.forked session.archived session.disconnected
turn.queued turn.started turn.steered turn.interrupting turn.completed turn.failed
turn.interrupted pipeline.started pipeline.checkpoint pipeline.completed pipeline.failed stage.started
stage.completed task.started task.completed message.started message.delta message.completed plan.updated policy.updated
command.started command.output command.completed tool.started tool.completed tool.failed
file.changed workspace.snapshot validation.started validation.completed gate.verdict
approval.requested approval.resolved approval.expired actor.spawned actor.status
actor.completed review.finding artifact.created usage.updated runtime.warning runtime.error
runtime.unknown_item runtime.capabilities_changed
""".split())


def _event(**changes) -> RuntimeEvent:
    values = {
        "event_id": "evt_abc-123",
        "seq": 1,
        "occurred_at": "2026-08-21T12:00:00Z",
        "workspace_id": "ws_one",
        "session_id": "ses_one",
        "run_id": "run_one",
        "turn_id": "turn_one",
        "actor_id": "agent_main",
        "kind": "command.completed",
        "payload": {"exit_code": 0},
        "extensions": {"provider": "legacy"},
    }
    values.update(changes)
    return RuntimeEvent(**values)


def test_runtime_event_schema_round_trip_and_unknown_top_level_fields():
    raw = json.loads(_event().to_jsonl())
    raw["future_contract"] = {"revision": 2, "enabled": True}
    parsed = RuntimeEvent.from_jsonl(json.dumps(raw) + "\n")
    restored = json.loads(parsed.to_jsonl())
    assert parsed == _event()
    assert restored == raw
    assert parsed.unknown_fields == {"future_contract": raw["future_contract"]}
    assert "future_contract" not in parsed.extensions


def test_unknown_runtime_event_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown runtime event kind"):
        _event(kind="runtime.untrusted")


def test_runtime_event_kind_enum_exactly_matches_design():
    assert RUNTIME_EVENT_KINDS == EXPECTED_EVENT_KINDS
    for index, kind in enumerate(sorted(EXPECTED_EVENT_KINDS)):
        assert _event(event_id=f"evt_kind_{index}", kind=kind).kind == kind


@pytest.mark.parametrize("kind", [None, [], 7])
def test_non_string_runtime_event_kind_is_rejected_as_value_error(kind):
    with pytest.raises(ValueError, match="unknown runtime event kind"):
        _event(kind=kind)


@pytest.mark.parametrize("event_id", ["event_1", "evt_", "evt_has space", 7])
def test_invalid_event_id_is_rejected(event_id):
    with pytest.raises(ValueError, match="invalid event_id"):
        _event(event_id=event_id)


def test_sequence_tracker_rejects_out_of_order_event():
    tracker = EventSequenceTracker()
    assert tracker.accept(_event(seq=3)) is True
    with pytest.raises(ValueError, match="non-monotonic seq"):
        tracker.accept(_event(event_id="evt_late", seq=2))


def test_duplicate_event_id_is_detected_and_skipped_before_seq_check():
    tracker = EventSequenceTracker()
    event = _event(seq=3)
    assert tracker.accept(event) is True
    assert tracker.is_duplicate(event) is True
    assert is_duplicate(event, tracker.event_ids) is True
    assert tracker.accept(_event(seq=1)) is False


def test_sequence_is_scoped_per_run():
    tracker = EventSequenceTracker()
    assert tracker.accept(_event(seq=9)) is True
    assert tracker.accept(_event(
        event_id="evt_second_run", run_id="run_two", seq=1)) is True


def test_terminal_turn_state_rejects_every_transition():
    for current in TERMINAL_TURN_STATES:
        for next_state in TURN_STATES:
            with pytest.raises(ValueError, match="illegal turn transition"):
                turn_transition(current, next_state)


EXPECTED_TRANSITIONS = {
    TurnState.QUEUED: {
        TurnState.RUNNING, TurnState.INTERRUPTING, TurnState.FAILED,
    },
    TurnState.RUNNING: {
        TurnState.SUCCEEDED, TurnState.FAILED, TurnState.INTERRUPTING,
        TurnState.WAITING_APPROVAL, TurnState.DISCONNECTED, TurnState.UNKNOWN,
    },
    TurnState.INTERRUPTING: {TurnState.INTERRUPTED, TurnState.FAILED},
    TurnState.WAITING_APPROVAL: {
        TurnState.RUNNING, TurnState.DENIED, TurnState.INTERRUPTING,
        TurnState.DISCONNECTED, TurnState.UNKNOWN,
    },
    TurnState.DENIED: {TurnState.FAILED, TurnState.INTERRUPTED},
    TurnState.DISCONNECTED: {TurnState.RECONCILING},
    TurnState.RECONCILING: {
        TurnState.RUNNING, TurnState.UNKNOWN, TurnState.SUCCEEDED,
        TurnState.FAILED,
    },
    TurnState.SUCCEEDED: set(),
    TurnState.FAILED: set(),
    TurnState.INTERRUPTED: set(),
    TurnState.UNKNOWN: set(),
}
LEGAL_TRANSITIONS = [
    (current, next_state)
    for current, next_states in EXPECTED_TRANSITIONS.items()
    for next_state in next_states
]
ILLEGAL_TRANSITIONS = [
    (current, next_state)
    for current in TURN_STATES
    for next_state in TURN_STATES
    if next_state not in EXPECTED_TRANSITIONS[current]
]


@pytest.mark.parametrize(("current", "next_state"), LEGAL_TRANSITIONS)
def test_all_legal_turn_transitions(current, next_state):
    assert turn_transition(current, next_state) == next_state


@pytest.mark.parametrize(("current", "next_state"), ILLEGAL_TRANSITIONS)
def test_all_illegal_turn_transitions(current, next_state):
    with pytest.raises(ValueError, match="illegal turn transition"):
        turn_transition(current, next_state)


def test_transition_table_exactly_matches_contract():
    assert set(TURN_TRANSITIONS) == TURN_STATES
    assert {
        state: set(next_states) for state, next_states in TURN_TRANSITIONS.items()
    } == EXPECTED_TRANSITIONS


def test_oversized_payload_is_rejected():
    payload = {"text": "x" * (MAX_EVENT_PAYLOAD_BYTES + 1)}
    with pytest.raises(ValueError, match="payload exceeds"):
        _event(payload=payload)


def test_oversized_extensions_are_rejected():
    extensions = {"raw": "x" * (MAX_EVENT_EXTENSIONS_BYTES + 1)}
    with pytest.raises(ValueError, match="extensions exceeds"):
        _event(extensions=extensions)


@pytest.mark.parametrize(
    "line", ["{bad", "[]", "null", '{"future": NaN}', "{}\n{}\n"])
def test_bad_json_non_object_and_multiple_lines_are_rejected(line):
    with pytest.raises(ValueError):
        RuntimeEvent.from_jsonl(line)


def test_oversized_jsonl_line_is_rejected_before_parsing():
    line = "{" + "x" * MAX_EVENT_LINE_BYTES + "}"
    with pytest.raises(ValueError, match="line exceeds"):
        RuntimeEvent.from_jsonl(line)


def test_runtime_capabilities_have_legacy_safe_defaults():
    runtime = RuntimeCapabilities()
    legacy = {"message_stream", "interrupt"}
    rich = {
        "structured_events", "session_resume", "session_read", "session_list",
        "session_fork", "session_archive", "session_state", "session_stats",
        "session_compact",
        "turn_steer", "message_queue", "typed_approvals", "review", "subagents",
        "actor_control", "images", "usage", "instruction_sources", "model_catalog", "thinking_levels",
        "model_select", "thinking_select",
    }
    assert runtime.protocol_version == 1
    assert runtime.runtime == "legacy"
    assert set(runtime.capabilities) == rich | legacy
    assert runtime.capabilities["message_stream"] is True
    assert runtime.capabilities["interrupt"] is True
    assert all(runtime.capabilities[name] is False for name in rich)
    assert runtime.message_stream is True
    assert runtime.structured_events is False
    assert runtime.supports("unknown_future_capability") is False


def test_non_legacy_runtime_does_not_inherit_legacy_capabilities():
    runtime = RuntimeCapabilities(runtime="codex-app-server")
    assert runtime.message_stream is False
    assert runtime.interrupt is False


def test_unknown_runtime_capability_is_ignored():
    runtime = RuntimeCapabilities(capabilities={"future_capability": True})
    assert "future_capability" not in runtime.capabilities
    assert runtime.supports("future_capability") is False


def test_unsupported_protocol_versions_are_rejected():
    with pytest.raises(ValueError, match="unsupported schema_version"):
        _event(schema_version=2)
    with pytest.raises(ValueError, match="unsupported protocol_version"):
        RuntimeCapabilities(protocol_version=2)
