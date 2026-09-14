"""Real bounded input/output transfer through Hub, signed Fabric and Runner."""

import base64
import json
import sys

import pytest

from pbatch.hub_client import HubError
from pbatch.hub_compute_cli import parser
from pbatch.hub_workspace_cli import run
from pbatch.workspace_bundle import bundle_digest
from test_hub_compute_fabric import compute, fabric, wait_until  # noqa: F401
from test_hub_workspace import bundle, vault  # noqa: F401


def _ready(client, task_id):
    task = client.get(task_id)
    return task if task.get("workspace_result", {}).get("state") in {"ready", "failed", "unavailable"} else None


def test_vault_snapshot_remote_execution_outputs_and_safe_cli_restore(compute, vault, tmp_path):
    client, session, runtime, server, worker, root = compute
    runtime.connections["team"].archive = vault[0]
    client.client.timeout = 30
    script = ("from pathlib import Path\n"
              "Path('output.txt').write_bytes(Path('input.txt').read_bytes()+b' computed')\n"
              "print('workspace completed')\n")
    inputs = bundle({"job.py": script.encode(), "input.txt": b"token=selected-by-user"})
    snapshot = client.upload_snapshot(session, inputs, "snapshot")
    assert client.upload_snapshot(session, inputs, "snapshot") == snapshot
    assert client.snapshots(session) == [snapshot]
    (root / "output.txt").write_text("existing device file")
    task = client.submit(session, {"argv": [sys.executable, "job.py"], "timeout": 15,
                                  "workspace": {"snapshot_id": snapshot["snapshot_id"],
                                                "outputs": ["output.txt"]}}, "workspace-task")
    result = wait_until(lambda: _ready(client, task["task_id"]), timeout=30)
    assert result["state"] == "completed", result
    assert result["workspace_result"]["state"] == "ready", result
    assert result["workspace_result"]["input_sha256"] == bundle_digest(inputs)
    assert (root / "output.txt").read_text() == "existing device file"
    output = client.workspace_output(task["task_id"])
    assert base64.b64decode(output["bundle"]["files"][0]["content_b64"]) == b"token=selected-by-user computed"
    assert "content_b64" not in json.dumps(client.tasks(session))
    assert "test-vault-token" not in json.dumps(server.control.tasks)
    destination = tmp_path / "recovered"
    args = parser().parse_args(["workspace-output", task["task_id"], "--directory", str(destination),
                                "--input-sha256", snapshot["sha256"]])
    assert run(args, client)["file_count"] == 1
    assert (destination / "output.txt").read_bytes() == b"token=selected-by-user computed"
    with pytest.raises((ValueError, OSError)):
        run(args, client)
    assert len(server.control.tasks) == 1


def test_snapshot_http_size_exception_keeps_prompt_limit(compute, vault):
    client, session, runtime, *_ = compute
    runtime.connections["team"].archive = vault[0]
    client.client.timeout = 30
    large = bundle({f"data-{index}.bin": b"x" * 65536 for index in range(4)})
    snapshot = client.upload_snapshot(session, large, "large-upload")
    assert snapshot["file_count"] == 4 and snapshot["size"] > 65536
    with pytest.raises(ValueError, match="limit"):
        client.client._request("POST", "/sessions/" + session + "/turns", {"prompt": "x" * 65536})
    with pytest.raises(HubError) as error:
        client.upload_snapshot(session, bundle({"too-big.bin": b"x" * 65537}), "bad-upload")
    assert error.value.status == 400


def test_running_snapshot_cancellation_keeps_terminal_state_and_releases_slot(compute, vault):
    client, session, runtime, server, worker, root = compute
    runtime.connections["team"].archive = vault[0]
    marker = root / "snapshot-started"
    script = ("import sys,time\nfrom pathlib import Path\n"
              "Path(sys.argv[1]).write_text('started')\ntime.sleep(30)\n"
              "Path('late.txt').write_text('should not finish')\n")
    snapshot = client.upload_snapshot(session, bundle({"job.py": script.encode()}), "cancel-snapshot")
    task = client.submit(session, {"argv": [sys.executable, "job.py", str(marker)], "timeout": 40,
                                  "workspace": {"snapshot_id": snapshot["snapshot_id"],
                                                "outputs": ["late.txt"]}}, "cancel-workspace")
    wait_until(marker.exists)
    client.cancel(task["task_id"])
    result = wait_until(lambda: _ready(client, task["task_id"]), timeout=20)
    assert result["state"] == "cancelled" and result["workspace_result"]["state"] == "unavailable", result
    assert result["result"]["cancelled"]
    assert client.devices()[0]["reserved_cpu_cores"] == 0


def test_missing_declared_output_fails_instead_of_reporting_success(compute, vault):
    client, session, runtime, *_ = compute
    runtime.connections["team"].archive = vault[0]
    snapshot = client.upload_snapshot(session, bundle({}), "empty-snapshot")
    task = client.submit(session, {"argv": [sys.executable, "-c", "pass"], "timeout": 15,
                                  "workspace": {"snapshot_id": snapshot["snapshot_id"],
                                                "outputs": ["missing.txt"]}}, "missing-output")
    result = wait_until(lambda: _ready(client, task["task_id"]), timeout=20)
    assert result["state"] == "failed", result
    assert result["workspace_result"]["state"] == "unavailable", result
    assert client.devices()[0]["reserved_cpu_cores"] == 0
