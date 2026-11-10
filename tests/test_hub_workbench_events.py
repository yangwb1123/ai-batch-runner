"""Gateway workbench projections retain message identity and bounded progress evidence."""

import json

import pytest

from pbatch.hub_gateway_protocol import project_event_payload, VISIBLE_EVENTS
from pbatch.tui_protocol import RuntimeEvent
from test_hub_http import hub, publish, send, event
from test_hub_integration import fleet, _until


def runtime(kind, payload):
    return RuntimeEvent(event_id="evt_workbench", seq=1, occurred_at="now", workspace_id="workspace",
        session_id="session", run_id="run", turn_id="turn", actor_id="agent", kind=kind, payload=payload)


def test_interleaved_message_items_remain_distinguishable_without_exposing_provider_identifiers():
    kinds = ("message.started", "message.delta", "message.completed")
    first = [project_event_payload(runtime(kind, {"item_id": "token=private-item", "text": "first"}))[0]
             for kind in kinds]
    second = project_event_payload(runtime("message.delta", {"item_id": "second", "text": "second"}))[0]
    assert all(kind in VISIBLE_EVENTS for kind in kinds)
    assert len({row["item_id"] for row in first}) == 1
    assert first[0]["item_id"] != second["item_id"]
    assert "private-item" not in repr(first)
    assert first[0] == {"item_id": first[0]["item_id"]}
    assert "item_id" not in project_event_payload(runtime("message.delta", {"text": "legacy"}))[0]


def test_chunked_message_items_retain_one_identity_and_redact_across_chunk_boundaries():
    text = "x" * 4090 + " token=SECRET-VALUE " + "猫" * 5000
    rows = project_event_payload(runtime("message.completed", {"item_id": "one", "text": text}))
    assert len(rows) > 1 and len({row["item_id"] for row in rows}) == 1
    assert "SECRET-VALUE" not in "".join(row["text"] for row in rows)
    assert [row["chunk_index"] for row in rows] == list(range(len(rows)))
    assert all(row["chunk_count"] == len(rows) for row in rows)


def test_valid_runtime_long_message_identity_is_hashed_without_losing_distinction():
    first = project_event_payload(runtime("message.delta", {"item_id": "x" * 8000, "text": "one"}))[0]
    second = project_event_payload(runtime("message.delta", {"item_id": "x" * 7999 + "y", "text": "two"}))[0]
    assert first["item_id"] != second["item_id"]
    assert len(first["item_id"]) == 69


@pytest.mark.parametrize("value", [0, False, [], {}, None, 0.0])
def test_invalid_message_identity_never_becomes_an_implicit_legacy_message(value):
    with pytest.raises(ValueError, match="identity"):
        project_event_payload(runtime("message.delta", {"item_id": value, "text": "evidence"}))


def test_empty_message_identity_remains_compatible_with_legacy_events():
    row = project_event_payload(runtime("message.delta", {"item_id": "", "text": "legacy"}))[0]
    assert "item_id" not in row and row["text"] == "legacy"


@pytest.mark.parametrize("kind", ["command.started", "command.completed", "file.changed"])
def test_progress_events_publish_only_bounded_redacted_summaries(kind):
    payload = {"item_id": "command-id", "command": "token=SECRET cat project", "status": "completed",
               "exit_code": 7, "changes": [{"path": f"src/{i}.py", "kind": "update"} for i in range(20)],
               "aggregated_output": "PRIVATE OUTPUT", "access_token": "PRIVATE TOKEN"}
    rows = project_event_payload(runtime(kind, payload))
    assert kind in VISIBLE_EVENTS and len(rows) == 1
    row = rows[0]
    assert "PRIVATE" not in repr(row) and "SECRET" not in repr(row)
    assert len(row["text"].encode()) <= 4096 and row["state"] == "completed"
    assert len(json.dumps(row).encode()) < 32768
    if kind == "command.completed":
        assert row["exit_code"] == 7
    if kind == "file.changed":
        assert len(row["paths"]) == 16 and row["truncated"] is True


def test_progress_projection_preserves_unknown_exit_and_path_summary_without_file_reads():
    command = project_event_payload(runtime("command.completed", {"command": "run", "exit_code": True}))[0]
    assert command["exit_code"] is None
    file = project_event_payload(runtime("file.changed", {"path": "/not/read/from/disk"}))[0]
    assert file["paths"] == ["/not/read/from/disk"]
    assert file["text"] == "/not/read/from/disk"


def test_new_gateway_events_do_not_complete_a_turn(hub):
    call, store, _ = hub
    session, generation = publish(call)
    turn = send(call, session)[1]["data"]["turn_id"]
    event(call, session, turn, generation, kind="turn.running", event_id="run")
    for kind in ("message.started", "command.started", "command.completed", "file.changed"):
        assert event(call, session, turn, generation, kind=kind, event_id=kind,
                     payload={"text": "bounded evidence", "item_id": "item-a"})[0] == 200
    assert store.turn("team", turn)["state"] == "running"


def test_real_gateway_persists_interleaved_messages_and_progress_without_reexecution(fleet):
    user, create, _ = fleet
    gateway, provider, session = create("alpha", hold=True)
    turn = user.send_prompt(session, "Inspect safely", "workbench-once")["turn_id"]
    _until([gateway], lambda: bool(provider.prompts))
    provider.emit("message.started", {"item_id": "first"})
    provider.emit("message.delta", {"item_id": "first", "text": "working"})
    provider.emit("message.started", {"item_id": "second"})
    provider.emit("message.completed", {"item_id": "second", "text": "password=secret-result answer"})
    provider.emit("command.started", {"item_id": "command", "command": "token=secret-command inspect"})
    provider.emit("file.changed", {"changes": [{"path": "src/main.py", "kind": "update"}]})
    provider.emit("message.completed", {"item_id": "first", "text": "inspection complete"})
    _until([gateway], lambda: len(user.session_events(session)) == 9)
    rows = user.session_events(session)
    assert len({row["payload"]["item_id"] for row in rows if row["kind"].startswith("message.")}) == 2
    assert "secret-result" not in repr(rows) and "secret-command" not in repr(rows)
    assert user.get_turn(turn)["state"] == "running"
    history = user._request("GET", f"/sessions/{session}/turns?limit=20")
    assert history[0]["prompt"] == "Inspect safely"
    provider.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(turn)["state"] == "completed")
    assert provider.prompts == ["Inspect safely"]
