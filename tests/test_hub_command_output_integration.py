"""Real Hub/Gateway/AgentSession publication and exact-turn ownership; synthetic provider."""

from dataclasses import replace
from functools import partial

import pytest

from pbatch.hub_gateway_config import HubGatewayConfig
from pbatch.tui_protocol import RuntimeEvent
import test_hub_integration as integration
from test_hub_integration import fleet, _until


@pytest.mark.parametrize("enabled", [False, True])
def test_gateway_command_snapshot_optin_reaches_only_owned_turns(fleet, monkeypatch, enabled):
    monkeypatch.setattr(integration, "HubGatewayConfig",
                        partial(HubGatewayConfig, publish_command_output=enabled))
    user, create, _ = fleet
    gateway, provider, session = create("alpha", hold=True)
    first = user.send_prompt(session, "first", "output-first")["turn_id"]
    _until([gateway], lambda: len(provider.prompts) == 1)
    old_handle = provider.handle
    provider.emit("command.output", {"item_id": "cmd", "aggregated_output": "FIRST token=secret-one"})
    provider.emit("command.output", {"item_id": "cmd", "delta": "PRIVATE DELTA"})
    provider.emit("command.completed", {"item_id": "cmd", "command": "run", "exit_code": 7,
                  "aggregated_output": "END password=secret-two"})
    _until([gateway], lambda: any(row["kind"] == "command.completed"
                                 for row in user.session_events(session)))
    assert user.get_turn(first)["state"] == "running"
    provider.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(first)["state"] == "completed")
    first_rows = [row for row in user.session_events(session) if row["turn_id"] == first]
    outputs = [row for row in first_rows if "output_text" in row["payload"]]
    assert len(outputs) == (2 if enabled else 0)
    assert "secret-one" not in repr(first_rows) and "secret-two" not in repr(first_rows)
    assert "PRIVATE DELTA" not in repr(first_rows)
    assert user.get_turn(first)["state"] == "completed"
    second = user.send_prompt(session, "second", "output-second")["turn_id"]
    _until([gateway], lambda: len(provider.prompts) == 2)
    _foreign_outputs(provider, old_handle)
    provider.emit("command.output", {"item_id": "cmd", "aggregated_output": "SECOND"})
    provider.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(second)["state"] == "completed")
    second_rows = [row for row in user.session_events(session) if row["turn_id"] == second]
    assert "FOREIGN" not in repr(second_rows) and "FIRST" not in repr(second_rows)
    output_rows = [row for row in second_rows if row["kind"] == "command.output"]
    assert len(output_rows) == int(enabled)
    if enabled:
        assert output_rows[0]["payload"]["output_text"] == "SECOND"
    assert provider.prompts == ["first", "second"]


def test_large_snapshot_survives_real_outbox_and_http_bounds(fleet, monkeypatch):
    monkeypatch.setattr(integration, "HubGatewayConfig",
                        partial(HubGatewayConfig, publish_command_output=True))
    user, create, _ = fleet
    gateway, provider, session = create("alpha", hold=True)
    turn = user.send_prompt(session, "bounded", "output-large")["turn_id"]
    _until([gateway], lambda: bool(provider.prompts))
    provider.emit("command.completed", {"item_id": "cmd", "command": "🧪" * 4000,
                  "exit_code": 0, "aggregated_output": "🧪" * 9000})
    _until([gateway], lambda: any(row["kind"] == "command.completed"
                                 for row in user.session_events(session)))
    row, = [row for row in user.session_events(session) if row["kind"] == "command.completed"]
    assert len(row["payload"]["output_text"].encode()) <= 8192
    assert row["payload"]["output_truncated"] is True
    assert user.get_turn(turn)["state"] == "running"
    provider.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(turn)["state"] == "completed")


def _foreign_outputs(provider, previous):
    current = provider.handle
    sample = RuntimeEvent(event_id="evt_foreign", seq=999, occurred_at="now",
        workspace_id=provider.session.workspace_id, session_id=current.session_id,
        run_id=current.run_id, turn_id=current.turn_id, actor_id="agent", kind="command.output",
        payload={"item_id": "cmd", "aggregated_output": "FOREIGN"})
    for index, fields in enumerate((
            {"run_id": previous.run_id, "turn_id": previous.turn_id},
            {"workspace_id": "foreign"}, {"session_id": "foreign"},
            {"run_id": "foreign"}, {"turn_id": "foreign"})):
        provider.queue.put(replace(sample, event_id=f"evt_foreign_{index}", **fields))
