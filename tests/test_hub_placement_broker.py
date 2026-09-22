"""Active-turn diagnostics travel through the real Unix capability and Hub HTTP boundary."""

import json
import shlex
from types import SimpleNamespace

import pytest

from pbatch.hub_client import HubError
from pbatch.hub_compute_broker import broker_request, turn_tools
from pbatch.hub_compute_cli import main
from test_hub_compute_transport import transport  # noqa: F401


def active(transport):
    turn = transport.user.send_prompt(transport.session, "inspect placement", "placement-turn")["turn_id"]
    transport.hub.append_event("team", "source", 1, {
        "event_id": "placement-running", "session_id": transport.session, "turn_id": turn,
        "kind": "turn.running", "payload": {}})
    gateway = SimpleNamespace(options=SimpleNamespace(compute_tools=True),
                              instance_id="source", generation=1, client=transport.gateway)
    return gateway, turn


def capability(prompt):
    words = shlex.split(next(line for line in prompt.splitlines() if "--via" in line))
    return words[words.index("--via") + 1]


def test_active_turn_cli_reads_one_page_without_keys_and_revokes_on_turn_end(transport, monkeypatch, capsys):
    gateway, turn = active(transport)
    reads = []

    def missing_inventory(tenant, *, target_ids):
        reads.append((tenant, target_ids))
        return []

    monkeypatch.setattr(transport.compute, "inventory", missing_inventory)
    with turn_tools(gateway, SimpleNamespace(hub_id=transport.session), turn, "original") as prompt:
        assert "placement TASK_ID [--after DEVICE_ID]" in prompt
        path = capability(prompt)
        task = broker_request(path, "submit", {"argv": ["true"]}, "submission-key")
        arguments = ["--via", path, "placement", task["task_id"], "--limit", "1", "--json"]
        assert main(arguments) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["task_id"] == task["task_id"] and result["state"] == "queued"
        assert result["devices"] == [{"device_id": "device-b", "instance_id": "remote",
                                        "eligible": False, "reasons": ["inventory_missing"]}]
        assert reads == [("team", ["device-b"])]
        transport.hub.cancel_turn("team", turn, "user")
        with pytest.raises(HubError, match="active running turn"):
            broker_request(path, "placement", {"task_id": task["task_id"]})
    with pytest.raises((OSError, ValueError)):
        broker_request(path, "placement", {"task_id": task["task_id"]})


def test_active_turn_can_reschedule_queued_task_without_a_key(transport):
    gateway, turn = active(transport)
    with turn_tools(gateway, SimpleNamespace(hub_id=transport.session), turn, "original") as prompt:
        path = capability(prompt)
        task = broker_request(path, "submit", {"argv": ["true"]}, "submission-key")
        moved = broker_request(path, "reschedule",
                               {"task_id": task["task_id"], "target_device_id": "device-b"})
        assert moved["task_id"] == task["task_id"]
        assert moved["target_device_id"] == "device-b"
        with pytest.raises(HubError) as error:
            broker_request(path, "reschedule",
                           {"task_id": task["task_id"], "target_device_id": ""}, "bad-key")
        assert error.value.code == "invalid_request"
        with pytest.raises(HubError) as error:
            broker_request(path, "reschedule", {"task_id": task["task_id"]})
        assert error.value.code == "invalid_request"


def test_active_turn_can_explicitly_retry_lost_task(transport):
    gateway, turn = active(transport)
    with turn_tools(gateway, SimpleNamespace(hub_id=transport.session), turn, "original") as prompt:
        path = capability(prompt)
        task = broker_request(path, "submit", {"argv": ["true"]}, "submission-key")
        transport.compute.store.update("team", task["task_id"], {}, state="lost")
        with pytest.raises(HubError) as error:
            broker_request(path, "retry", {"task_id": task["task_id"]})
        assert error.value.code == "invalid_request"
        retried = broker_request(path, "retry", {
            "task_id": task["task_id"], "confirm_duplicate": True})
        assert retried["state"] == "queued"


@pytest.mark.parametrize("payload,key", [
    ({"task_id": "task-1"}, "forbidden-key"), ({"task_id": "task-1", "scope": "all"}, ""),
    ({"task_id": "task-1", "limit": True}, ""), ({"task_id": "task-1", "after": " x"}, ""),
])
def test_invalid_broker_diagnostic_rejected_before_capability_read(payload, key):
    with pytest.raises(ValueError, match="placement|identifier"):
        broker_request("/does-not-exist/capability", "placement", payload, key)
