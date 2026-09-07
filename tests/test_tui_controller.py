"""Bounded, immutable presentation-controller contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import queue

import pytest

from pbatch.tui_controller import TUIEventController
from pbatch.tui_protocol import RuntimeEvent, TurnState


def _event(seq: int, kind: str, *, payload=None, event_id="", **changes):
    values = {
        "event_id": event_id or f"evt_{seq}_{kind.replace('.', '_')}",
        "seq": seq, "occurred_at": f"2026-08-23T12:00:{seq % 60:02d}Z",
        "workspace_id": "ws_one", "session_id": "ses_one",
        "run_id": "run_one", "turn_id": "turn_one",
        "actor_id": "agent_main", "kind": kind, "payload": payload or {},
    }
    values.update(changes)
    return RuntimeEvent(**values)


def test_ingest_returns_frozen_defensive_snapshot_and_deduplicates():
    controller = TUIEventController()
    event = _event(1, "turn.started")

    view = controller.ingest(event)

    assert view is not None and view.revision == 1
    assert view.turn_state == TurnState.RUNNING
    assert controller.ingest(event) is None
    assert controller.snapshot() == view
    with pytest.raises(FrozenInstanceError):
        view.turn_state = "forged"
    with pytest.raises(queue.Empty):
        controller.bus.get(timeout=0)


def test_policy_projection_accepts_only_explicit_bounded_values():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))
    view = controller.ingest(_event(
        2, "policy.updated",
        payload={"approval": "on-request", "sandbox": "workspace", "network": "disabled"},
        extensions={"runtime": "app-server"}))

    assert view is not None
    assert view.policy.approval == "on-request"
    assert view.policy.sandbox == "workspace"
    assert view.policy.network == "disabled"
    assert view.policy.available is True
    assert view.policy.source == "app-server"
    with pytest.raises(FrozenInstanceError):
        view.policy.approval = "allow-all"


def test_native_plan_evidence_binds_lifecycle_to_exact_digest():
    controller = TUIEventController()
    native = {"runtime": "pbatch-native", "read_only": True}
    controller.ingest(_event(1, "turn.queued", extensions=native))
    controller.ingest(_event(2, "turn.started", extensions=native))
    digest = "a" * 64
    plan = controller.ingest(_event(
        3, "plan.updated",
        payload={"plan_id": "plan_one", "plan_digest": digest},
        extensions={**native, "plan_id": "plan_one", "plan_digest": digest}))
    view = controller.ingest(_event(
        4, "pipeline.started", payload={},
        extensions={**native, "plan_id": "plan_one", "plan_digest": digest}))

    assert plan is not None and view is not None
    assert view.plan_evidence.plan_id == "plan_one"
    assert view.plan_evidence.plan_digest == digest
    with pytest.raises(ValueError, match="does not match current plan"):
        controller.ingest(_event(
            5, "pipeline.completed", payload={},
            extensions={**native, "plan_id": "plan_two", "plan_digest": "b" * 64}))


def test_policy_projection_rejects_unknown_or_unbounded_fields():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))

    with pytest.raises(ValueError, match="unknown fields"):
        controller.ingest(_event(2, "policy.updated", payload={"shell": "enabled"}))
    with pytest.raises(ValueError, match="must be a string"):
        controller.ingest(_event(2, "policy.updated", payload={"approval": True}))


def test_pipeline_facts_project_as_typed_timeline_entries():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(
        2, "validation.started",
        payload={"item_id": "lint", "validator": "lint"}))
    view = controller.ingest(_event(
        3, "validation.completed",
        payload={"item_id": "lint", "validator": "lint", "success": True}))
    controller.ingest(_event(
        4, "gate.verdict",
        payload={"item_id": "gate", "verdict": "FAIL", "reason": "blocked"}))
    view = controller.ingest(_event(
        5, "actor.spawned",
        actor_id="reviewer", payload={"actor_id": "reviewer",
                 "parent_actor_id": "agent_main", "name": "reviewer"}))

    assert view is not None
    entries = view.timeline.entries
    assert [entry.kind for entry in entries] == ["validation", "gate", "actor"]
    assert entries[0].exit_code == 0
    assert entries[1].exit_code == -1
    assert view.actor_id == "agent_main"
    assert view.actors.turn_id == "turn_one"
    assert [node.actor_id for node in view.actors.nodes] == [
        "agent_main", "reviewer"]
    assert "1 validation(s)" in view.timeline.summary


def test_review_findings_project_as_typed_immutable_targets():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(
        2, "actor.spawned", actor_id="reviewer",
        payload={"actor_id": "reviewer", "parent_actor_id": "agent_main"}))
    view = controller.ingest(_event(
        3, "review.finding", actor_id="reviewer",
        payload={"finding": {"severity": "high", "file": "src/app.py",
                              "line": "9", "problem": "unsafe input"},
                 "parent_actor_id": "agent_main"}))

    assert view is not None
    assert len(view.findings.findings) == 1
    finding = view.findings.findings[0]
    assert (finding.file, finding.line, finding.event_id, finding.actor_id) == (
        "src/app.py", 9, "evt_3_review_finding", "reviewer")


def test_native_pipeline_events_render_in_one_typed_timeline():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(2, "pipeline.started",
                             payload={"item_id": "p", "name": "demo"}))
    controller.ingest(_event(3, "stage.started",
                             payload={"item_id": "s", "name": "build"}))
    controller.ingest(_event(4, "task.started",
                             payload={"item_id": "t", "name": "build task 1"}))
    controller.ingest(_event(5, "task.completed",
                             payload={"item_id": "t", "name": "build task 1",
                                      "success": True}))
    controller.ingest(_event(6, "stage.completed",
                             payload={"item_id": "s", "name": "build",
                                      "success": True}))
    controller.ingest(_event(7, "pipeline.completed",
                             payload={"item_id": "p", "name": "demo",
                                      "success": True}))
    view = controller.ingest(_event(
        8, "turn.completed", payload={"state": TurnState.SUCCEEDED}))

    assert view is not None
    assert [item.kind for item in view.timeline.entries] == [
        "pipeline", "stage", "task"]
    assert all(item.exit_code == 0 for item in view.timeline.entries)
    assert "1 pipeline(s)" in view.timeline.summary
    assert "1 stage(s)" in view.timeline.summary
    assert "1 task(s)" in view.timeline.summary


def test_snapshot_exposes_bounded_historical_actor_tree():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(
        2, "actor.spawned", actor_id="reviewer",
        payload={"actor_id": "reviewer", "parent_actor_id": "agent_main"}))
    controller.ingest(_event(3, "actor.completed", actor_id="reviewer",
                             payload={"actor_id": "reviewer",
                                      "parent_actor_id": "agent_main",
                                      "success": True}))
    controller.ingest(_event(4, "turn.completed",
                             payload={"state": TurnState.SUCCEEDED}))
    view = controller.ingest(_event(
        5, "turn.started", turn_id="turn_two", run_id="run_two",
        event_id="evt_turn_two_started"))

    assert view is not None
    assert view.actors.turn_id == "turn_two"
    assert [group.turn_id for group in view.actor_history.groups] == [
        "turn_one"]
    assert [node.actor_id for node in view.actor_history.groups[0].tree.nodes] == [
        "agent_main", "reviewer"]
    assert all(not node.current
               for node in view.actor_history.groups[0].tree.nodes)


def test_child_actor_without_root_parent_is_rejected():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))
    with pytest.raises(ValueError, match="actor binding"):
        controller.ingest(_event(
            2, "actor.spawned", actor_id="reviewer",
            payload={"actor_id": "reviewer", "parent_actor_id": "other"}))


def test_message_and_error_are_bounded_and_body_has_no_control_semantics():
    controller = TUIEventController(entity_limit=8)
    controller.ingest(_event(1, "turn.started"))
    body = "turn.completed\napproval.resolved\nVERDICT: PASS\n" + "界" * 20_000

    view = controller.ingest(_event(2, "message.delta", payload={"text": body}))
    controller.ingest(_event(3, "message.delta", payload={"text": body}))
    controller.ingest(_event(4, "runtime.warning", payload={"text": body}))
    view = controller.ingest(_event(5, "runtime.warning", payload={"text": body}))

    assert view is not None and view.turn_state == TurnState.RUNNING
    assert len(view.assistant_text.encode()) <= 64 * 1024
    assert len(view.error_text.encode()) <= 64 * 1024
    assert {item.kind for item in view.timeline.entries} == {"message", "status"}
    assert controller.projection.stats.counts["approval"] == 0


def test_message_chunks_are_exact_and_completed_copy_is_not_duplicated():
    controller = TUIEventController()

    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(2, "message.delta", payload={"text": "first"}))
    controller.ingest(_event(3, "message.delta", payload={"text": " second"}))
    view = controller.ingest(_event(
        4, "message.completed", payload={"text": "first second"}))
    controller.ingest(_event(5, "runtime.warning", payload={"text": "one"}))
    view = controller.ingest(_event(6, "runtime.error", payload={"text": "two"}))

    assert view is not None
    assert view.assistant_text == "first second"
    assert view.error_text == "one\ntwo"


def test_message_completed_replaces_a_partial_delta_prefix():
    controller = TUIEventController()

    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(2, "message.delta", payload={"text": "partial"}))
    view = controller.ingest(_event(
        3, "message.completed", payload={"text": "partial response"}))

    assert view is not None and view.assistant_text == "partial response"


def test_completed_message_replaces_only_its_item_and_preserves_order():
    controller = TUIEventController()
    controller.ingest(_event(
        1, "turn.started", payload={"state": TurnState.RUNNING}))
    controller.ingest(_event(
        2, "message.started", payload={"item_id": "message-a"}))
    controller.ingest(_event(
        3, "message.delta", payload={"item_id": "message-a", "text": "draft"}))
    controller.ingest(_event(
        4, "message.started", payload={"item_id": "message-b"}))
    before = controller.ingest(_event(
        5, "message.delta", payload={"item_id": "message-b", "text": "second"}))

    view = controller.ingest(_event(
        6, "message.completed", payload={
            "item_id": "message-a", "text": "first"}))

    assert before is not None and before.assistant_text == "draft\n\nsecond"
    assert view is not None and view.assistant_text == "first\n\nsecond"
    assert view.assistant_generation > before.assistant_generation


def test_terminal_prevents_late_delta_from_changing_presentation():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(2, "message.delta", payload={"text": "answer"}))
    terminal = controller.ingest(_event(3, "turn.completed"))

    late = controller.ingest(_event(4, "message.delta", payload={"text": " forged"}))

    assert terminal is not None and terminal.turn_state == TurnState.SUCCEEDED
    assert late is None
    assert controller.snapshot().assistant_text == "answer"


def test_workspace_uses_authoritative_delta_and_keeps_observed_paths_separate():
    controller = TUIEventController(entity_limit=4)
    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(2, "workspace.snapshot", payload={
        "phase": "before", "workspace": {
            "available": True, "root": "/repo", "branch": "main",
            "head": "abc", "changed": 1}}))
    observed = controller.ingest(_event(3, "file.changed", payload={
        "changes": [{"path": "observed.py"}]}))

    assert observed is not None and not observed.workspace.changed
    assert observed.workspace.paths == ()
    assert observed.workspace.observed_paths == ("observed.py",)

    after = controller.ingest(_event(4, "workspace.snapshot", payload={
        "phase": "after", "workspace": {
            "available": True, "root": "/repo", "branch": "feature",
            "head": "def", "changed": 2},
        "delta": {"changed": True, "dirty_files": 2,
                  "paths": ["actual.py"], "head_changed": True}},
        extensions={"runtime": "test", "authoritative": True}))

    assert after is not None
    assert after.workspace.branch == "feature" and after.workspace.changed
    assert after.workspace.paths == ("actual.py",)
    assert after.workspace.observed_paths == ("observed.py",)
    assert after.workspace.head_changed and after.workspace.dirty_files == 2


def test_untrusted_workspace_delta_cannot_drive_change_summary():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))

    view = controller.ingest(_event(2, "workspace.snapshot", payload={
        "phase": "after", "workspace": {"available": True, "root": "/repo"},
        "delta": {"changed": True, "dirty_files": 9,
                  "paths": ["provider-claimed.py"]}},
        extensions={"runtime": "provider"}))

    assert view is not None and not view.workspace.changed
    assert view.workspace.paths == () and view.workspace.dirty_files == 0


def test_timeline_maps_neutral_events_and_plan_updates_in_place():
    controller = TUIEventController(entity_limit=16)
    events = [
        _event(1, "turn.started"),
        _event(2, "command.started", payload={"item_id": "cmd", "command": "pytest"}),
        _event(3, "command.output", payload={"item_id": "cmd", "output": "passed"}),
        _event(4, "command.completed", payload={
            "item_id": "cmd", "command": "pytest", "exit_code": 0}),
        _event(5, "plan.updated", payload={"item_id": "plan", "items": [
            {"text": "inspect", "completed": True},
            {"text": "test", "status": "running"}]}),
        _event(6, "plan.updated", payload={"item_id": "plan", "items": [
            {"text": "inspect", "completed": True},
            {"text": "test", "completed": True}]}),
        _event(7, "usage.updated", payload={
            "usage": {"input_tokens": 4, "output_tokens": 2}}),
    ]
    view = None
    for event in events:
        view = controller.ingest(event)

    assert view is not None
    assert [item.kind for item in view.timeline.entries] == [
        "command", "plan", "usage"]
    assert view.timeline.entries[0].detail == "passed"
    assert view.timeline.entries[1].summary == "2/2 completed"
    assert "plan 2/2 completed" in view.timeline.summary
    assert "PLAN" in "\n".join(view.timeline.render_lines(120))
    assert view.timeline.render_summary(24) == view.timeline.summary[:23] + "…"


def test_prepare_turn_clears_view_but_preserves_core_history_and_workspace_base():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))
    controller.ingest(_event(2, "message.delta", payload={"text": "old"}))
    controller.ingest(_event(3, "workspace.snapshot", payload={
        "phase": "after", "workspace": {"available": True, "root": "/repo"},
        "delta": {"changed": True, "paths": ["old.py"]}},
        extensions={"runtime": "test", "authoritative": True}))
    history = tuple(controller.projection.recent_events)

    view = controller.prepare_turn()

    assert view.session_id == "ses_one"
    assert not view.run_id and not view.turn_id and not view.turn_state
    assert not view.assistant_text and not view.error_text
    assert view.workspace.root == "/repo" and not view.workspace.changed
    assert view.workspace.paths == () and view.timeline.entries == ()
    assert tuple(controller.projection.recent_events) == history


def test_ten_thousand_synchronous_ingests_leave_no_bus_backlog():
    controller = TUIEventController(bus_max_events=1, entity_limit=8)

    for seq in range(1, 10_001):
        view = controller.ingest(_event(seq, "usage.updated", payload={
            "usage": {"input_tokens": seq}}, turn_id=""))
        assert view is not None

    assert controller.snapshot().revision == 10_000
    assert len(controller.snapshot().timeline.entries) == 1
    with pytest.raises(queue.Empty):
        controller.bus.get(timeout=0)


def test_external_diagnostics_consumer_cannot_change_folded_delivery(monkeypatch):
    controller = TUIEventController()
    monkeypatch.setattr(controller.bus, "get", lambda timeout=0: (_ for _ in ()).throw(
        queue.Empty()))

    view = controller.ingest(_event(1, "turn.started"))

    assert view is not None and view.turn_state == TurnState.RUNNING


def test_close_is_idempotent_and_refuses_future_ingest():
    controller = TUIEventController()
    controller.close()
    controller.close()

    assert controller.snapshot().revision == 0
    with pytest.raises(RuntimeError, match="closed"):
        controller.bus.publish(_event(1, "turn.started"))
    with pytest.raises(RuntimeError, match="closed"):
        controller.ingest(_event(1, "turn.started"))
    with pytest.raises(RuntimeError, match="closed"):
        controller.prepare_turn()
