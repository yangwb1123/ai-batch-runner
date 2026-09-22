"""Placement reads across real Hub, signed Fabric RPC, Runner and process execution."""

import json
import sys

from pbatch import hub_compute_cli
from pbatch.fabric_rpc import RpcClient
from test_hub_compute_fabric import compute, fabric, wait_until, _terminal  # noqa: F401


def test_busy_device_diagnosis_then_single_execution_after_release(compute):
    client, session, runtime, server, worker, directory = compute
    blocking = [sys.executable, "-c", "from pathlib import Path; import time; "
                "Path('started').write_text('yes'); deadline=time.monotonic()+25; "
                "exec(\"while not Path('release').exists() and time.monotonic()<deadline: time.sleep(.05)\")"]
    first = client.submit(session, {"argv": blocking, "timeout": 30}, "occupy-worker")
    wait_until(lambda: (directory / "started").exists())
    command = [sys.executable, "-c", "from pathlib import Path; "
               "p=Path('once'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]
    second = client.submit(session, {"argv": command, "timeout": 15}, "waiting-worker")
    try:
        before = set(server.control.tasks)
        for _ in range(2):
            page = client.placement(second["task_id"])
            assert page["state"] == "queued" and page["next_cursor"] == ""
            assert page["devices"][0]["device_id"] == worker["device_id"]
            assert not page["devices"][0]["eligible"]
            assert "device_busy" in page["devices"][0]["reasons"]
        assert set(server.control.tasks) == before and len(before) == 1
        assert len(client.tasks(session)) == 2
        assert not (directory / "once").exists()
    finally:
        (directory / "release").write_text("yes")
    assert wait_until(lambda: _terminal(client, first["task_id"]))["state"] == "completed"
    result = wait_until(lambda: _terminal(client, second["task_id"]))
    assert result["state"] == "completed", result
    assert (directory / "once").read_text() == "x"
    assert len(server.control.tasks) == 2 and result["result"]["evidence_digest"]
    final = client.placement(second["task_id"])
    assert final["state"] == "completed" and final["devices"] == []
    assert final["next_cursor"] == ""


def test_cli_reads_cordoned_task_without_reserving_then_cancel_skips_inventory(compute, monkeypatch, capsys):
    client, session, runtime, server, worker, _directory = compute
    connection = runtime.connection("team")
    worker_client = RpcClient(connection.url, worker["secret"], worker["device_id"])
    assert worker_client.cordon().get("ok")
    task = client.submit(session, {"argv": [sys.executable, "-c", "print('unused')"]}, "cordoned")
    monkeypatch.setattr(hub_compute_cli.HubClient, "from_env", lambda *_args, **_kwargs: client.client)
    args = ["placement", task["task_id"], "--limit", "1", "--json"]
    assert hub_compute_cli.main(args) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["task_id"] == task["task_id"] and page["state"] == "queued"
    assert not page["devices"][0]["eligible"] and page["devices"][0]["reasons"]
    assert server.control.tasks == {} and len(client.tasks(session)) == 1
    assert client.cancel(task["task_id"])["state"] == "cancelled"

    def unexpected_inventory(*_args, **_kwargs):
        raise AssertionError("terminal placement must not contact Fabric")

    monkeypatch.setattr(runtime, "inventory", unexpected_inventory)
    assert client.placement(task["task_id"])["devices"] == []
    assert server.control.tasks == {}
