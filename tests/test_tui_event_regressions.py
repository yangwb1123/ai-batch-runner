"""Regression contracts for trusted TUI event projection boundaries."""

from __future__ import annotations

import pytest

from pbatch.tui_controller import TUIEventController
from pbatch.tui_events import Projection
from pbatch.tui_protocol import RuntimeEvent


def _event(seq: int, kind: str, *, payload=None, extensions=None, **changes):
    values = {
        "event_id": f"evt_regression_{seq}_{kind.replace('.', '_')}",
        "seq": seq,
        "occurred_at": f"2026-08-23T12:00:{seq:02d}Z",
        "workspace_id": "ws_regression",
        "session_id": "ses_regression",
        "run_id": "run_regression",
        "turn_id": "turn_regression",
        "actor_id": "agent_main",
        "kind": kind,
        "payload": payload or {},
        "extensions": extensions or {},
    }
    values.update(changes)
    return RuntimeEvent(**values)


def test_controller_folds_event_bus_canonical_copy(monkeypatch):
    controller = TUIEventController()
    assert controller.ingest(_event(1, "turn.started")) is not None
    publish = controller.bus.publish

    def publish_then_mutate_producer_event(event):
        accepted = publish(event)
        event.payload["text"] = "mutated after canonical publish"
        return accepted

    monkeypatch.setattr(controller.bus, "publish", publish_then_mutate_producer_event)
    view = controller.ingest(_event(
        2, "message.delta", payload={"text": "canonical answer"}))

    assert view is not None
    assert view.assistant_text == "canonical answer"


_TURN_SCOPED_KINDS = (
    "message.started",
    "message.delta",
    "message.completed",
    "plan.updated",
    "command.started",
    "command.output",
    "command.completed",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "file.changed",
    "validation.started",
    "validation.completed",
    "gate.verdict",
    "approval.requested",
    "approval.resolved",
    "approval.expired",
    "review.finding",
    "artifact.created",
)

_TURN_LIFECYCLE_KINDS = (
    "turn.queued",
    "turn.started",
    "turn.steered",
    "turn.interrupting",
    "turn.completed",
    "turn.failed",
    "turn.interrupted",
)


@pytest.mark.parametrize("kind", _TURN_SCOPED_KINDS + _TURN_LIFECYCLE_KINDS)
def test_turn_bound_event_rejects_empty_turn_id(kind):
    projection = Projection()

    with pytest.raises(ValueError, match="turn"):
        projection.register_event(_event(1, kind, turn_id=""))

    assert not projection.sessions
    assert not projection.turns
    assert not projection.recent_events


def test_untrusted_before_snapshot_cannot_clear_authoritative_after_delta():
    controller = TUIEventController()
    assert controller.ingest(_event(1, "turn.started")) is not None
    authoritative = controller.ingest(_event(
        2,
        "workspace.snapshot",
        payload={
            "phase": "after",
            "workspace": {
                "available": True,
                "root": "/repo",
                "branch": "main",
                "head": "abc",
                "changed": 2,
            },
            "delta": {
                "changed": True,
                "dirty_files": 2,
                "paths": ["trusted.py"],
                "head_changed": True,
            },
        },
        extensions={"runtime": "tui-backend", "authoritative": True},
    ))
    assert authoritative is not None and authoritative.workspace.changed

    view = controller.ingest(_event(
        3,
        "workspace.snapshot",
        payload={
            "phase": "before",
            "workspace": {
                "available": True,
                "root": "/repo",
                "branch": "provider-claim",
                "head": "forged",
                "changed": 0,
            },
        },
        extensions={"runtime": "provider"},
    ))

    assert view is not None
    assert view.workspace.phase == "after"
    assert view.workspace.changed
    assert view.workspace.dirty_files == 2
    assert view.workspace.paths == ("trusted.py",)
    assert view.workspace.head_changed


def test_evicted_turn_does_not_release_its_run_workspace_binding():
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
    assert "turn_first" not in projection.turns

    with pytest.raises(ValueError, match="run workspace/session binding"):
        projection.register_event(_event(
            3, "turn.started", event_id="evt_rebind_old_run",
            workspace_id="ws_second", session_id="ses_second",
            run_id="run_first", turn_id="turn_rebound"))


def test_empty_run_id_is_not_bound_across_session_level_events():
    projection = Projection()
    assert projection.register_event(_event(
        1, "session.created", event_id="evt_empty_run_one",
        workspace_id="ws_one", session_id="ses_one", run_id="", turn_id=""))
    assert projection.register_event(_event(
        2, "session.created", event_id="evt_empty_run_two",
        workspace_id="ws_two", session_id="ses_two", run_id="", turn_id=""))
    assert "" not in projection._run_bindings


@pytest.mark.parametrize("warning", ["single failure", "single failure\n"])
def test_failed_reason_does_not_repeat_an_identical_runtime_warning(warning):
    controller = TUIEventController()
    assert controller.ingest(_event(1, "turn.started")) is not None
    assert controller.ingest(_event(
        2, "runtime.warning", payload={"text": warning})) is not None

    view = controller.ingest(_event(
        3, "turn.failed", payload={"reason": "single failure"}))

    assert view is not None
    assert view.error_text == warning
