"""Typed actor lineage and bounded Agents-tree projection contracts."""

from __future__ import annotations

import pytest

from pbatch.tui_actor_tree import (
    ActorAuthority, ActorHistorySnapshot, ActorNode, ActorState,
    ActorTreeSnapshot, actor_authority_payload, authorize_actor_control,
    build_actor_history, build_actor_tree, dispatch_actor_control,
    normalize_actor_authority, render_actor_history, render_actor_tree,
    root_actor_authority_payload,
)
from pbatch.tui_events import Projection
from pbatch.tui_protocol import RuntimeCapabilities, RuntimeEvent
from pbatch.tui_runtime import (
    ActorInterruptRequest, ActorSteerRequest, RuntimeInfo, UnsupportedOperation,
)


def _event(seq: int, kind: str, *, actor_id: str = "agent_main",
           payload: dict | None = None, turn_id: str = "turn_one",
           event_id: str = "") -> RuntimeEvent:
    return RuntimeEvent(
        event_id=event_id or f"evt_actor_{seq}_{kind.replace('.', '_')}",
        seq=seq, occurred_at=f"2026-08-23T12:00:{seq:02d}Z",
        workspace_id="ws_one", session_id="ses_one", run_id="run_one",
        turn_id=turn_id, actor_id=actor_id, kind=kind,
        payload=payload or {})


def test_projection_accepts_nested_actor_lineage_and_keeps_root_identity():
    projection = Projection()
    assert projection.register_event(_event(1, "turn.started"))
    assert projection.register_event(_event(
        2, "actor.spawned", actor_id="reviewer",
        payload={"actor_id": "reviewer", "parent_actor_id": "agent_main",
                 "name": "Security reviewer", "role": "review"}))
    assert projection.register_event(_event(
        3, "actor.spawned", actor_id="checker",
        payload={"actor_id": "checker", "parent_actor_id": "reviewer",
                 "name": "Test checker"}))
    assert projection.register_event(_event(
        4, "actor.status", actor_id="checker",
        payload={"actor_id": "checker", "parent_actor_id": "reviewer",
                 "phase": "running"}))

    tree = build_actor_tree(projection.actors.values(), turn_id="turn_one")

    assert [node.actor_id for node in tree.nodes] == [
        "agent_main", "reviewer", "checker"]
    assert [node.parent_actor_id for node in tree.nodes] == [
        "", "agent_main", "reviewer"]
    assert [node.depth for node in tree.nodes] == [0, 1, 2]
    assert all(node.current for node in tree.nodes)
    assert projection.current_turn("run_one").actor_id == "agent_main"

    lines = render_actor_tree(tree, 120)
    assert lines[0] == "ACTOR TREE"
    assert any("Security reviewer [reviewer]" in line for line in lines)
    assert any("  ● Test checker [checker]" in line for line in lines)


def test_actor_authority_requires_explicit_and_parent_bound_claims():
    inherited = normalize_actor_authority({
        "state": "inherited", "source_actor_id": "parent",
        "capabilities": ["read", "read", {"not": "a capability"}],
        "scope": "workspace"}, "parent")

    assert inherited == ActorAuthority(
        "INHERITED", "parent", ("read",), "workspace")
    assert normalize_actor_authority({
        "state": "inherited", "source_actor_id": "other"}, "parent").state == "UNKNOWN"
    assert actor_authority_payload({
        "state": "declared", "capabilities": ["write"]}) == {
        "state": "DECLARED", "source_actor_id": "", "capabilities": ["write"],
        "scope": ""}
    assert actor_authority_payload({"state": "inherited"}, "parent") == {}
    assert root_actor_authority_payload({
        "turn": {"actorAuthority": {
            "state": "declared", "capabilities": ["read"]}}}, True) == {
        "state": "DECLARED", "source_actor_id": "",
        "capabilities": ["read"], "scope": ""}
    assert root_actor_authority_payload({
        "actorAuthority": {"state": "declared", "capabilities": ["read"]}},
        False) == {}
    parent = ActorAuthority("DECLARED", "", ("read", "write"), "workspace")
    assert normalize_actor_authority(
        {"state": "inherited", "source_actor_id": "parent",
         "capabilities": ["read"], "scope": "workspace"},
        "parent", parent).capabilities == ("read",)
    assert normalize_actor_authority(
        {"state": "inherited", "source_actor_id": "parent",
         "capabilities": ["admin"], "scope": "workspace"},
        "parent", parent).state == "UNKNOWN"
    inherited_parent = ActorAuthority(
        "INHERITED", "grandparent", ("read",), "workspace")
    assert normalize_actor_authority(
        {"state": "inherited", "source_actor_id": "parent",
         "capabilities": ["read"], "scope": "workspace"},
        "parent", inherited_parent).state == "INHERITED"


def test_projection_enforces_parent_authority_for_inherited_children():
    projection = Projection()
    projection.register_event(_event(
        1, "turn.started", payload={"authority": {
            "state": "declared", "capabilities": ["read"],
            "scope": "workspace"}}))
    projection.register_event(_event(
        2, "actor.spawned", actor_id="reviewer",
        payload={"actor_id": "reviewer", "parent_actor_id": "agent_main",
                 "authority": {"state": "inherited",
                                "source_actor_id": "agent_main",
                                "capabilities": ["read"],
                                "scope": "workspace"}}))
    projection.register_event(_event(
        3, "actor.spawned", actor_id="forged",
        payload={"actor_id": "forged", "parent_actor_id": "reviewer",
                 "authority": {"state": "inherited",
                                "source_actor_id": "reviewer",
                                "capabilities": ["write"],
                                "scope": "workspace"}}))

    assert projection.actors[("turn_one", "reviewer")].authority.state == "INHERITED"
    assert projection.actors[("turn_one", "forged")].authority.state == "UNKNOWN"


def test_actor_history_groups_retained_turns_and_excludes_current():
    states = [
        ActorState("root-old", "", "ws", "ses", "run-old", "turn-old",
                   status="SUCCEEDED", terminal=True,
                   last_event_at="2026-08-23T12:00:01Z"),
        ActorState("child-old", "root-old", "ws", "ses", "run-old", "turn-old",
                   name="old reviewer", status="SUCCEEDED", terminal=True,
                   success=True, last_event_at="2026-08-23T12:00:02Z"),
        ActorState("root-current", "", "ws", "ses", "run-current", "turn-current",
                   status="RUNNING", last_event_at="2026-08-23T12:00:03Z"),
        ActorState("root-new", "", "ws", "ses", "run-new", "turn-new",
                   status="FAILED", terminal=True,
                   last_event_at="2026-08-23T12:00:04Z"),
    ]

    history = build_actor_history(states, current_turn_id="turn-current")

    assert [group.turn_id for group in history.groups] == ["turn-new", "turn-old"]
    assert history.groups[0].run_id == "run-new"
    assert [node.actor_id for node in history.groups[1].tree.nodes] == [
        "root-old", "child-old"]
    assert all(not node.current for group in history.groups
               for node in group.tree.nodes)
    assert render_actor_history(history, 120)[0] == "ACTOR HISTORY"


def test_actor_history_is_bounded_and_rejects_untrusted_iterables():
    states = [ActorState(
        f"actor-{index}", "", "ws", "ses", f"run-{index}", f"turn-{index}",
        status="FAILED", terminal=True,
        last_event_at=f"2026-08-23T12:00:{index:02d}Z")
        for index in range(5)]

    history = build_actor_history(
        [object(), *states], current_turn_id="", limit=2, node_limit=1)

    assert isinstance(history, ActorHistorySnapshot)
    assert len(history.groups) == 2
    assert history.truncated and history.skipped == 4


def test_child_actor_lifecycle_requires_prior_spawn_and_matching_parent():
    projection = Projection()
    projection.register_event(_event(1, "turn.started"))

    with pytest.raises(ValueError, match="prior actor.spawned"):
        projection.register_event(_event(
            2, "actor.status", actor_id="reviewer",
            payload={"actor_id": "reviewer", "parent_actor_id": "agent_main"}))
    with pytest.raises(ValueError, match="actor binding"):
        projection.register_event(_event(
            3, "actor.spawned", actor_id="reviewer",
            payload={"actor_id": "reviewer", "parent_actor_id": "unknown"}))

    assert not projection.actors.get(("turn_one", "reviewer"))
    assert len(projection.recent_events) == 1


def test_actor_ids_are_scoped_to_turn_and_completed_nodes_are_not_current():
    projection = Projection()
    projection.register_event(_event(1, "turn.started"))
    projection.register_event(_event(2, "actor.spawned", actor_id="reviewer",
                                     payload={"parent_actor_id": "agent_main"}))
    projection.register_event(_event(
        3, "actor.completed", actor_id="reviewer",
        payload={"parent_actor_id": "agent_main", "success": True}))
    projection.register_event(_event(4, "turn.completed"))
    projection.register_event(_event(
        5, "turn.started", turn_id="turn_two", event_id="evt_actor_second_start"))
    projection.register_event(_event(
        6, "actor.spawned", actor_id="reviewer", turn_id="turn_two",
        event_id="evt_actor_second_spawn",
        payload={"parent_actor_id": "agent_main"}))

    first = build_actor_tree(projection.actors.values(), turn_id="turn_one")
    current = build_actor_tree(projection.actors.values(), turn_id="turn_two")
    assert first.nodes[0].actor_id == "agent_main"
    assert first.nodes[1].success is True and not first.nodes[1].current
    assert [node.actor_id for node in current.nodes] == [
        "agent_main", "reviewer"]
    assert all(node.current for node in current.nodes)


class _ActorControlRuntime:
    def __init__(self, enabled=True):
        self.calls = []
        self.info = RuntimeInfo(
            "test", 1, RuntimeCapabilities(
                runtime="test", capabilities={"actor_control": enabled}))

    def runtime_info(self):
        return self.info

    def steer_actor(self, request):
        self.calls.append(request)

    def interrupt_actor(self, request):
        self.calls.append(request)


def _active_tree():
    return ActorTreeSnapshot((
        ActorNode("agent_main", "", status="RUNNING", current=True),
        ActorNode("reviewer", "agent_main", status="RUNNING", current=True),
    ), turn_id="turn_one")


def test_actor_control_requires_capability_and_exact_current_identity():
    runtime = _ActorControlRuntime(enabled=False)
    with pytest.raises(UnsupportedOperation, match="actor_control"):
        dispatch_actor_control(runtime, _active_tree(), "turn_one", "reviewer",
                               "stop")
    assert runtime.calls == []

    runtime = _ActorControlRuntime()
    dispatch_actor_control(runtime, _active_tree(), "turn_one", "reviewer",
                           "steer", "please inspect the test")
    assert runtime.calls == [ActorSteerRequest(
        "turn_one", "reviewer", "please inspect the test")]
    dispatch_actor_control(runtime, _active_tree(), "turn_one", "reviewer", "stop")
    assert runtime.calls[-1] == ActorInterruptRequest("turn_one", "reviewer")

    with pytest.raises(ValueError, match="identity"):
        authorize_actor_control(
            runtime.info.capabilities, _active_tree(), "turn_two", "reviewer", "stop")
    with pytest.raises(ValueError, match="identity"):
        dispatch_actor_control(runtime, _active_tree(), "turn_one", "missing", "stop")


def test_actor_control_rejects_unknown_action_and_empty_steer():
    runtime = _ActorControlRuntime()
    with pytest.raises(ValueError, match="unknown actor control"):
        dispatch_actor_control(runtime, _active_tree(), "turn_one", "reviewer", "kill")
    with pytest.raises(ValueError, match="steer text"):
        dispatch_actor_control(runtime, _active_tree(), "turn_one", "reviewer", "steer")
    assert runtime.calls == []


def test_actor_tree_copy_is_bounded_and_skips_invalid_state_records():
    states = [ActorState(
        actor_id=f"actor-{index}", parent_actor_id="", workspace_id="ws",
        session_id="ses", run_id="run", turn_id="turn")
        for index in range(5)]
    snapshot = build_actor_tree(states + [object()], turn_id="turn", limit=3)

    assert isinstance(snapshot, ActorTreeSnapshot)
    assert len(snapshot.nodes) == 3
    assert snapshot.truncated and snapshot.skipped == 3
    assert len("\n".join(render_actor_tree(snapshot, 20)).splitlines()) <= 6
