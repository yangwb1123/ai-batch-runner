"""Gateway reads only events belonging to its exact AgentSession prompt stream."""

from dataclasses import replace

import pytest

from pbatch.tui_protocol import RuntimeEvent
from test_hub_integration import fleet, _until


def enqueue(runtime, handle, kind, payload=None, *, actor="agent", workspace=None):
    runtime.seq += 1
    runtime.queue.put(RuntimeEvent(
        event_id=f"evt_binding_{runtime.seq}", seq=runtime.seq, occurred_at="now",
        workspace_id=runtime.session.workspace_id if workspace is None else workspace, session_id=handle.session_id,
        run_id=handle.run_id, turn_id=handle.turn_id, actor_id=actor,
        kind=kind, payload=payload or {}))


def next_turn(user, create):
    gateway, runtime, session = create("alpha", hold=True)
    first = user.send_prompt(session, "First", "first-binding")["turn_id"]
    _until([gateway], lambda: len(runtime.prompts) == 1)
    previous = runtime.handle
    runtime.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(first)["state"] == "completed" and not gateway._turn_threads)
    second = user.send_prompt(session, "Second", "second-binding")["turn_id"]
    _until([gateway], lambda: len(runtime.prompts) == 2)
    return gateway, runtime, session, previous, second


@pytest.mark.parametrize("wrong", ["old_turn", "old_run", "other_session"])
def test_stale_provider_command_cannot_enter_a_new_hub_turn(fleet, wrong):
    user, create, _ = fleet
    gateway, runtime, session, previous, second = next_turn(user, create)
    handle = previous if wrong == "old_turn" else replace(runtime.handle,
        **({"run_id": previous.run_id} if wrong == "old_run" else {"session_id": "foreign-session"}))
    enqueue(runtime, handle, "turn.started")
    enqueue(runtime, handle, "command.completed", {"item_id": "stale", "command": "STALE PRIVATE COMMAND", "exit_code": 0})
    runtime.emit("command.completed", {"item_id": "current", "command": "current command", "exit_code": 7})
    _until([gateway], lambda: any(row["payload"].get("text") == "current command" for row in user.session_events(session)))
    rows = user.session_events(session)
    assert not any("STALE" in str(row) for row in rows)
    assert user.get_turn(second)["state"] == "running"
    runtime.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(second)["state"] == "completed")


def test_stale_terminal_cannot_complete_or_interrupt_current_turn(fleet):
    user, create, _ = fleet
    gateway, runtime, session, previous, second = next_turn(user, create)
    enqueue(runtime, previous, "turn.interrupted", {"reason": "stale cancellation"})
    runtime.emit("command.completed", {"item_id": "current", "command": "current survives", "exit_code": 0})
    _until([gateway], lambda: any(row["payload"].get("text") == "current survives" for row in user.session_events(session)))
    assert user.get_turn(second)["state"] == "running"
    assert not any(row["turn_id"] == second and row["kind"] == "turn.interrupted" for row in user.session_events(session))
    runtime.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(second)["state"] == "completed")


def test_terminal_only_stream_and_same_turn_child_actor_are_supported(fleet, monkeypatch):
    user, create, _ = fleet
    gateway, runtime, session = create("alpha", hold=True)
    emit = runtime.emit
    monkeypatch.setattr(runtime, "emit", lambda kind, payload=None: None if kind == "turn.started" else emit(kind, payload))
    turn = user.send_prompt(session, "No started event", "no-start-binding")["turn_id"]
    _until([gateway], lambda: bool(runtime.prompts))
    enqueue(runtime, runtime.handle, "command.completed", {"item_id": "child", "command": "child command", "exit_code": 0}, actor="child-agent")
    runtime.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(turn)["state"] == "completed")
    assert any(row["payload"].get("text") == "child command" for row in user.session_events(session))


def test_unknown_events_queued_before_current_stream_do_not_acquire_its_authority(fleet, monkeypatch):
    user, create, _ = fleet
    gateway, runtime, session = create("alpha", hold=True)
    emit = runtime.emit

    def late_before_start(kind, payload=None):
        if kind == "turn.started":
            stale = replace(runtime.handle, run_id="old-run", turn_id="old-turn")
            enqueue(runtime, stale, "command.completed", {"item_id": "old", "command": "STALE BEFORE START", "exit_code": 0})
        return emit(kind, payload)

    monkeypatch.setattr(runtime, "emit", late_before_start)
    turn = user.send_prompt(session, "Current", "pre-start-binding")["turn_id"]
    _until([gateway], lambda: bool(runtime.prompts))
    runtime.emit("message.completed", {"text": "current reply"})
    runtime.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(turn)["state"] == "completed")
    assert not any("STALE" in str(row) for row in user.session_events(session))


@pytest.mark.parametrize("failure", [None, "invalid runtime event"])
def test_broken_provider_stream_fails_closed_without_a_terminal(fleet, failure):
    user, create, _ = fleet
    gateway, runtime, session = create("alpha", hold=True)
    turn = user.send_prompt(session, "Inspect", "stream-failure")["turn_id"]
    _until([gateway], lambda: bool(runtime.prompts))
    runtime.emit("message.completed", {"text": "PASS, all done"})
    runtime.queue.put(failure)
    _until([gateway], lambda: user.get_turn(turn)["state"] == "failed")
    assert not any(row["kind"] == "turn.completed" for row in user.session_events(session))


def test_missing_terminal_from_session_stream_is_reported_without_replay(fleet, monkeypatch):
    user, create, _ = fleet
    gateway, runtime, session = create("alpha", hold=True)
    managed = next(iter(gateway._sessions.values()))
    monkeypatch.setattr(managed.session, "prompt_events", lambda prompt: iter(()))
    turn = user.send_prompt(session, "Inspect", "missing-terminal")["turn_id"]
    _until([gateway], lambda: user.get_turn(turn)["state"] == "failed")
    rows = user.session_events(session)
    assert rows[-1]["payload"]["reason"] == "runtime_missing_terminal_event"
    assert runtime.prompts == []


def test_gateway_consumer_failure_closes_observation_without_replaying_runtime(fleet, monkeypatch):
    user, create, _ = fleet
    gateway, runtime, session = create("alpha", hold=True)
    managed = next(iter(gateway._sessions.values()))
    publish = gateway._on_event

    def fail_consumer(owner, event, turn_id):
        if event.kind == "command.completed":
            raise RuntimeError("consumer failure")
        return publish(owner, event, turn_id)

    monkeypatch.setattr(gateway, "_on_event", fail_consumer)
    turn = user.send_prompt(session, "Inspect", "consumer-failure")["turn_id"]
    _until([gateway], lambda: bool(runtime.prompts))
    runtime.emit("command.completed", {"command": "trigger", "exit_code": 0})
    _until([gateway], lambda: user.get_turn(turn)["state"] == "failed" and not gateway._turn_threads)
    assert managed.session.state["busy"] and managed.session._active.discarded
    for _ in range(300):
        runtime.emit("message.delta", {"text": "not replayed"})
    runtime.emit("turn.completed")
    _until([gateway], lambda: not managed.session.state["busy"])
    assert runtime.prompts == ["Inspect"]
    assert user.get_turn(turn)["state"] == "failed"


@pytest.mark.parametrize("field,value", [("session_id", "foreign-session"), ("run_id", ""), ("turn_id", "")])
def test_invalid_provider_handle_scope_fails_the_hub_turn(fleet, monkeypatch, field, value):
    user, create, _ = fleet
    gateway, runtime, session = create("alpha", hold=True)
    start = runtime.start_turn

    def invalid_handle(request):
        runtime.handle = replace(start(request), **{field: value})
        runtime.emit("turn.completed")
        return runtime.handle

    monkeypatch.setattr(runtime, "start_turn", invalid_handle)
    turn = user.send_prompt(session, "Inspect", "invalid-provider-scope")["turn_id"]
    _until([gateway], lambda: user.get_turn(turn)["state"] == "failed")
    assert runtime.prompts == ["Inspect"]
    assert not any(row["kind"] == "turn.completed" for row in user.session_events(session))
    assert user.session_events(session)[-1]["payload"]["reason"] == "InvalidRuntimeTurnIdentity"


def test_foreign_workspace_evidence_and_terminal_never_enter_the_owned_turn(fleet):
    user, create, _ = fleet
    gateway, runtime, session = create("alpha", hold=True)
    turn = user.send_prompt(session, "Inspect", "workspace-scope")["turn_id"]
    _until([gateway], lambda: bool(runtime.prompts))
    enqueue(runtime, runtime.handle, "gate.verdict", {"gate": "FOREIGN GATE", "verdict": "PASS"}, workspace="foreign-workspace")
    enqueue(runtime, runtime.handle, "turn.completed", workspace="foreign-workspace")
    runtime.emit("command.completed", {"command": "owned command", "exit_code": 0})
    _until([gateway], lambda: any(row["payload"].get("text") == "owned command" for row in user.session_events(session)))
    assert user.get_turn(turn)["state"] == "running"
    assert not any("FOREIGN GATE" in str(row) for row in user.session_events(session))
    runtime.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(turn)["state"] == "completed")


def test_wrong_handle_with_correct_terminal_fails_instead_of_waiting_forever(fleet, monkeypatch):
    user, create, _ = fleet
    gateway, runtime, session = create("alpha", hold=True)
    start = runtime.start_turn

    def wrong_returned_handle(request):
        handle = start(request)
        runtime.emit("turn.completed")
        return replace(handle, session_id="foreign-session")

    monkeypatch.setattr(runtime, "start_turn", wrong_returned_handle)
    turn = user.send_prompt(session, "Inspect", "wrong-returned-handle")["turn_id"]
    _until([gateway], lambda: user.get_turn(turn)["state"] == "failed")
    assert runtime.prompts == ["Inspect"]
    assert not any(row["kind"] == "turn.completed" for row in user.session_events(session))
    assert user.session_events(session)[-1]["payload"]["reason"] == "InvalidRuntimeTurnIdentity"


def test_invalid_scope_cleanup_failure_has_distinct_hub_diagnostic(fleet, monkeypatch):
    user, create, _ = fleet
    gateway, runtime, session = create("alpha", hold=True)
    start, close = runtime.start_turn, runtime.close
    managed = next(iter(gateway._sessions.values()))
    close_calls = []

    def invalid_handle(request):
        handle = start(request)
        runtime.emit("turn.completed")
        return replace(handle, session_id="foreign-session")

    def failed_close():
        close_calls.append(True)
        raise OSError("cleanup unavailable")

    monkeypatch.setattr(runtime, "start_turn", invalid_handle)
    monkeypatch.setattr(runtime, "close", failed_close)
    try:
        turn = user.send_prompt(session, "Inspect", "cleanup-failure")["turn_id"]
        _until([gateway], lambda: user.get_turn(turn)["state"] == "failed")
        assert user.session_events(session)[-1]["payload"]["reason"] == "OwnedRuntimeCleanupFailed"
        assert runtime.prompts == ["Inspect"] and managed.session.state["closed"]
        with pytest.raises(RuntimeError, match="closed"):
            managed.session.prompt("must not replay", timeout=0.05)
        request = user.close_session(session, idempotency_key="close-after-cleanup-failed")
        request_id = request["request_id"]
        _until([gateway], lambda: user.session_close_request(request_id)["status"] in {"closed", "failed"})
        receipt = user.session_close_request(request_id)
        assert receipt["status"] == "failed" and receipt["error_code"] == "session_close_failed"
        assert gateway._managed(managed.local_id) is managed and managed.closing
        assert gateway._store.session(managed.local_id)["status"] == "close_failed"
        assert runtime.prompts == ["Inspect"] and len(close_calls) == 1
    finally:
        monkeypatch.setattr(runtime, "close", close)
        close()
        managed.session._event_thread.join(1)
