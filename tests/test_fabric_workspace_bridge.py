"""Signed-HTTP and Runner bridge coverage for inline workspace tasks."""

from __future__ import annotations

import base64
import json
import sys

from pbatch import fabric_runner
from pbatch import fabric_runner_workspace
from pbatch.fabric_rpc import build_request
from pbatch.fabric_workspace import workspace_evidence_error
from pbatch.workspace_bundle import bundle_digest


def _workspace(content: bytes = b"seed", outputs=None) -> dict:
    bundle = {
        "schema": "pbatch.workspace.v1",
        "files": [{"path": "input.txt",
                   "content_b64": base64.b64encode(content).decode("ascii")}],
    }
    return {"bundle": bundle, "sha256": bundle_digest(bundle),
            "outputs": list(outputs or ["result.txt"])}


def _submit(client, cwd, workspace, request_id="workspace-one", argv=None):
    return client.submit_task(
        argv or [sys.executable, "-c",
                 "from pathlib import Path; Path('result.txt').write_text("
                 "Path('input.txt').read_text() + '!')"],
        cwd=str(cwd), timeout=20, mobility="pinned", request_id=request_id,
        workspace=workspace)


def test_signed_http_poll_exec_and_private_result_roundtrip(fabric_http, tmp_path):
    client, _key = fabric_http["register"]("workspace-roundtrip")
    workspace = _workspace()
    submitted = _submit(client, tmp_path, workspace)

    assert "bundle" not in submitted["task"]["workspace"]
    assert submitted["task"]["workspace"]["sha256"] == workspace["sha256"]
    polled = client.poll_tasks(limit=1)["tasks"][0]
    assert polled["workspace"]["bundle"] == workspace["bundle"]
    result = fabric_runner.execute_task(client, polled)

    assert result["accepted"] is True
    task = client.task_status(polled["task_id"])["task"]
    assert task["status"] == "done"
    assert task["evidence"]["workspace"]["input_sha256"] == workspace["sha256"]
    output = task["evidence"]["workspace"]["bundle"]
    assert output["files"][0]["path"] == "result.txt"
    assert base64.b64decode(output["files"][0]["content_b64"]) == b"seed!"
    staging = tmp_path / ".pbatch-workspaces"
    assert staging.is_dir()
    task_dirs = list(staging.iterdir())
    assert len(task_dirs) == 1 and list(task_dirs[0].iterdir()) == []


def test_workspace_submit_idempotency_binds_input_and_outputs(fabric_http, tmp_path):
    client, _key = fabric_http["register"]("workspace-idempotent")
    workspace = _workspace()
    first = _submit(client, tmp_path, workspace, "same-key")
    replay = _submit(client, tmp_path, workspace, "same-key")
    conflict = _submit(client, tmp_path, _workspace(outputs=["other.txt"]),
                        "same-key")

    assert first["task"]["task_id"] == replay["task"]["task_id"]
    assert replay["duplicate"] is True
    assert conflict["code"] == "idempotency_conflict"
    assert conflict["retryable"] is False


def test_batched_poll_claims_at_most_one_large_workspace(fabric_http, tmp_path):
    client, _key = fabric_http["register"]("workspace-poll-budget")
    paths = [f"{index:03}/{'x' * 240}/{'y' * 240}-{index:03}.bin"
             for index in range(128)]
    files = [{"path": path,
              "content_b64": base64.b64encode(b"x" * 2048).decode()}
             for path in paths]
    bundle = {"schema": "pbatch.workspace.v1", "files": files}
    workspace = {"bundle": bundle, "sha256": bundle_digest(bundle),
                 "outputs": paths}
    argv = [sys.executable, "-c", "pass", "z" * (45 * 1024)]
    for index in range(2):
        result = _submit(client, tmp_path, workspace, f"poll-budget-{index}",
                         argv)
        assert result["task"] is not None

    response = client.poll_tasks(limit=3)
    task_views = response["tasks"]

    assert len(task_views) == 1
    assert len(task_views[0]["workspace"]["bundle"]["files"]) == 128
    control = fabric_http["httpd"].control
    queued = next(task for task in control.tasks.values()
                  if task["status"] == "queued")
    candidate = control._task_view(queued, include_workspace_input=True)
    would_be_batch = {"tasks": [task_views[0], candidate],
                      "server_time": response["server_time"],
                      "cordoned": False, "draining": False}
    assert len(json.dumps(would_be_batch).encode()) > 1024 * 1024
    statuses = [task["status"] for task in control.tasks.values()]
    assert statuses.count("leased") == 1
    assert statuses.count("queued") == 1


def test_workspace_requires_advertised_runner_and_valid_digest(fabric_http,
                                                               tmp_path):
    client, _key = fabric_http["register"]("workspace-capability")
    fabric_http["httpd"].control.devices[client.device_id][
        "runner_capabilities"] = []
    denied = _submit(client, tmp_path, _workspace(), "missing-cap")
    assert denied["task"] is None
    assert denied["retryable"] is False
    assert "lacks task.workspace.v1" in denied["error"]

    invalid = _workspace()
    invalid["sha256"] = "0" * 64
    malformed = _submit(client, tmp_path, invalid, "bad-digest")
    assert malformed["task"] is None
    assert malformed["code"] == "invalid_request"


def test_workspace_capture_failure_cannot_report_success(fabric_http, tmp_path):
    client, _key = fabric_http["register"]("workspace-missing-output")
    task = _submit(
        client, tmp_path, _workspace(), "missing-output",
        [sys.executable, "-c", "print('finished without result')"])["task"]
    claimed = client.poll_tasks(limit=1)["tasks"][0]

    result = fabric_runner.execute_task(client, claimed)

    assert result["accepted"] is True
    final = client.task_status(task["task_id"])["task"]
    assert final["status"] == "failed"
    assert final["evidence"]["result"] == "failed"
    assert final["evidence"]["workspace"]["bundle"]["files"] == []


def test_staging_error_checks_remote_cancel_even_without_monitor_signal(
        monkeypatch):
    class Monitor:
        def __init__(self, *_args):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    submitted = []
    monkeypatch.setattr(fabric_runner_workspace, "TaskMonitor", Monitor)
    monkeypatch.setattr(fabric_runner_workspace, "preflight_gpu",
                        lambda *_args: (None, ""))
    monkeypatch.setattr(fabric_runner_workspace, "prepare_workspace",
                        lambda _task: (None, None, "staging failed", None))
    monkeypatch.setattr(fabric_runner_workspace, "report_cancel_if_requested",
                        lambda *_args: {"accepted": True, "status": "cancelled"})

    result = fabric_runner_workspace.execute_workspace_task(
        object(), {"task_id": "task-cancel", "status": "leased"}, None,
        start_task=lambda *_args: True,
        submit_evidence=lambda *args: submitted.append(args),
        authorize_task_grant=lambda *_args: (True, []))

    assert result == {"accepted": True, "status": "cancelled"}
    assert submitted == []


def test_workspace_rejects_result_bundle_digest_or_input_mismatch():
    workspace = _workspace()
    evidence = {
        "result": "passed", "input_digest": workspace["sha256"],
        "workspace": {"input_sha256": workspace["sha256"],
                      "output_sha256": "0" * 64,
                      "bundle": {"schema": "pbatch.workspace.v1", "files": []}},
    }

    assert "output digest" in workspace_evidence_error(workspace, evidence)
    evidence["workspace"]["output_sha256"] = bundle_digest(evidence[
        "workspace"]["bundle"])
    evidence["input_digest"] = "f" * 64
    assert "input digest" in workspace_evidence_error(workspace, evidence)


def test_workspace_evidence_body_stays_below_fabric_http_limit(
    fabric_http, tmp_path):
    client, _key = fabric_http["register"]("workspace-bounded-output")
    outputs = [f"{index:03}/{'é' * 127}/{'ø' * 126}"
               for index in range(128)]
    workspace = _workspace(outputs=outputs)
    command = ("import sys; from pathlib import Path; "
               "[(Path(p).parent.mkdir(parents=True, exist_ok=True), "
               "Path(p).write_bytes(b'a' * 2048)) for p in sys.argv[1:129]]; "
               "sys.stdout.buffer.write(b'\\x00' * 1000000); "
               "sys.stderr.buffer.write(b'\\x00' * 1000000)")
    submitted = _submit(client, tmp_path, workspace, "bounded-output",
                        [sys.executable, "-c", command, *outputs])
    claimed = client.poll_tasks(limit=1)["tasks"][0]
    requests = []
    post = client._post

    def observe(path, payload, timeout=5.0):
        if path == "/evidence":
            request = build_request(client.secret, "POST", path, payload)
            requests.append(len(json.dumps(request).encode()))
        return post(path, payload, timeout)

    client._post = observe
    result = fabric_runner.execute_task(client, claimed)

    assert submitted["task"] is not None
    assert result["accepted"] is True
    assert requests and max(requests) < 1024 * 1024
    final = client.task_status(claimed["task_id"])["task"]
    assert final["evidence"]["output_overflow"] is True
    packed = final["evidence"]["workspace"]["bundle"]["files"]
    assert [item["path"] for item in packed] == outputs
    assert sum(len(base64.b64decode(item["content_b64"]))
               for item in packed) == 256 * 1024
