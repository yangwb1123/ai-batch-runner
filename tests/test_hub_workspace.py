"""Workspace ownership, pinned Vault bytes and archive recovery at the Hub boundary.

Fabric substitutions here are read-only probes; HubStore owns task history and audit events.
"""

import base64
from copy import deepcopy
from dataclasses import replace
import http.server
import json
import threading

import pytest

from pbatch.hub_compute import HubCompute
from pbatch.hub_compute_cli import _submission, parser
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import PREFIX
from pbatch.hub_store import HubStore, HubStoreError
from pbatch.hub_vault import VaultArchiveSettings, VaultEvidenceArchive
from pbatch.workspace_bundle import bundle_digest
from test_hub_compute import _delegate, _running_turn, submit, system  # noqa: F401
from test_hub_vault import _VaultHandler, _VaultState


def bundle(files=None):
    files = {"input.txt": b"credential-shaped content remains exact: token=abc"} if files is None else files
    return {"schema": "pbatch.workspace.v1", "files": [
        {"path": path, "content_b64": base64.b64encode(data).decode()} for path, data in sorted(files.items())]}


@pytest.fixture
def vault(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _VaultHandler)
    server.daemon_threads = True
    server.state = _VaultState()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = tmp_path / "vault.token"
    token.write_text("test-vault-token")
    token.chmod(0o600)
    archive = VaultEvidenceArchive(VaultArchiveSettings(
        f"http://127.0.0.1:{server.server_port}", "team", str(token)))
    yield archive, server.state
    server.shutdown()
    server.server_close()
    thread.join(5)


@pytest.fixture
def workspace(system, vault):
    system.connection.archive = vault[0]
    original = system.fabric.inventory

    def devices(ids):
        result = original(ids)
        result["capabilities"].append("hub.compute.workspace.v1")
        for row in result["devices"]:
            row["runner_capabilities"] = ["task.workspace.v1"]
        return result

    system.fabric.inventory = devices
    return system


def upload(value, files=None, key="upload", user=None, session=None):
    path = PREFIX + "/sessions/" + (session or value.session["session_id"]) + "/snapshots"
    return value.service.dispatch("POST", path, {}, {"bundle": bundle(files)}, user or value.user, key)[1]["data"]


def task_body(snapshot):
    return {"argv": ["python", "job.py"], "workspace": {
        "snapshot_id": snapshot["snapshot_id"], "outputs": ["output.txt"]}}


def finish(value, task_id, *, corruption=None):
    private = value.compute.store.get("team", task_id, private=True)
    output = bundle({"output.txt": b"exact result"})
    proof = {"input_sha256": private["workspace_result"]["input_sha256"],
             "output_sha256": bundle_digest(output), "bundle": output}
    if corruption:
        proof.update(corruption)
    value.fabric.tasks[task_id].update(status="done", evidence={"exit_code": 0, "workspace": proof})
    value.compute._reconcile(private)
    return value.compute.store.get("team", task_id, private=True)


def test_snapshot_is_pinned_scoped_idempotent_and_metadata_only(workspace, vault):
    value = workspace
    vault[1].drop_put_ack = True
    first = upload(value)
    assert upload(value) == first and vault[1].put_count == 1
    assert first["sha256"] == bundle_digest(bundle())
    assert "content_b64" not in json.dumps(first)
    with pytest.raises(HubStoreError, match="different content"):
        upload(value, {"other.txt": b"changed"})
    path = PREFIX + "/sessions/" + value.session["session_id"] + "/snapshots"
    assert value.service.dispatch("GET", path, {}, {}, value.user)[1]["data"] == [first]
    for user in (replace(value.user, scopes=frozenset()), replace(value.user, instances=frozenset({"other"}))):
        with pytest.raises(HubAccessError):
            value.service.dispatch("GET", path, {}, {}, user)
    other = value.hub.upsert_session("team", "source", 1, {
        "local_session_id": "other", "project_id": "project", "controllable": True})
    with pytest.raises(HubStoreError):
        value.service.dispatch("POST", PREFIX + "/sessions/" + other["session_id"] + "/tasks",
                               {}, task_body(first), value.user, "other-task")
    assert any(row["kind"] == "workspace.ready" for row in value.hub.events("team", value.session["session_id"]))


def test_output_archive_failure_retries_after_restart_without_execution(workspace, vault):
    value = workspace
    task = submit(value, task_body(upload(value)))
    value.compute.tick()
    private = finish(value, task["task_id"])
    assert private["state"] == "completed" and private["workspace_result"]["state"] == "pending"
    assert value.fabric.requests[0]["workspace"]["bundle"] == bundle()
    vault[1].versioning = False
    value.compute._reconcile(private)
    result = value.compute.store.get("team", task["task_id"])
    assert result["state"] == "completed" and result["archive_state"] == "failed"
    value.hub.close()
    value.hub = HubStore(value.hub.path)
    value.compute = HubCompute(value.hub, configured={"team": value.connection})
    vault[1].versioning = True
    private = value.compute.store.get("team", task["task_id"], private=True)
    value.compute._reconcile(private)
    result = value.compute.store.get("team", task["task_id"])
    assert result["workspace_result"]["state"] == "ready" and result["archive_state"] == "archived"
    assert len(value.fabric.requests) == 1
    assert "content_b64" not in json.dumps(value.compute.store.list("team", None))
    assert value.compute.workspace.download("team", result)["bundle"] == bundle({"output.txt": b"exact result"})


@pytest.mark.parametrize("corruption", [{"input_sha256": "0" * 64}, {"output_sha256": "0" * 64},
                                        {"bundle": bundle({"unexpected.txt": b"x"})}])
def test_corrupt_execution_output_never_reports_success(workspace, corruption):
    task = submit(workspace, task_body(upload(workspace)))
    workspace.compute.tick()
    result = finish(workspace, task["task_id"], corruption=corruption)
    assert result["state"] == "failed"
    assert result["workspace_result"]["state"] == "failed"
    assert result["error"] == "workspace_evidence_invalid"


def test_delegated_upload_is_bound_to_active_turn_and_user_scope(workspace):
    turn = _running_turn(workspace)
    snapshot = _delegate(workspace, turn, "snapshot-upload", {"bundle": bundle()})
    assert _delegate(workspace, turn, "snapshots") == [snapshot]
    task = _delegate(workspace, turn, "submit", task_body(snapshot))
    assert task["workspace_result"]["input_sha256"] == snapshot["sha256"]
    workspace.hub.cancel_turn("team", turn, "alice")
    with pytest.raises(HubAccessError, match="active running turn"):
        _delegate(workspace, turn, "snapshot-upload", {"bundle": bundle()})


def test_no_vault_and_invalid_workspace_requests_fail_before_task_dispatch(system):
    with pytest.raises(HubAccessError, match="Vault"):
        upload(system)
    for flags in (["--snapshot-id", "ws-1", "--workdir", "sub"],
                  ["--output", "result.txt"], ["--snapshot-id", "ws-1", "--output", "../bad"]):
        args = parser().parse_args(["submit", "--argv-json", '["true"]', *flags])
        with pytest.raises(ValueError):
            _submission(args)
    assert not system.fabric.requests


def test_cancel_during_vault_input_read_prevents_first_remote_submission(workspace, monkeypatch):
    from pbatch.hub_vault_workspace import VaultWorkspaceArchive
    task = submit(workspace, task_body(upload(workspace)))
    original = VaultWorkspaceArchive.read

    def read_and_cancel(archive, ref):
        workspace.compute.store.cancel("team", task["task_id"])
        return original(archive, ref)

    monkeypatch.setattr(VaultWorkspaceArchive, "read", read_and_cancel)
    workspace.compute.tick()
    assert workspace.compute.store.get("team", task["task_id"])["state"] == "cancelled"
    assert not workspace.fabric.requests


def test_old_runner_cannot_be_selected_for_workspace(workspace):
    original = workspace.fabric.inventory

    def old(ids):
        result = original(ids)
        for row in result["devices"]:
            row.pop("runner_capabilities")
        return result

    workspace.fabric.inventory = old
    task = submit(workspace, task_body(upload(workspace)))
    workspace.compute.tick()
    assert workspace.compute.store.get("team", task["task_id"])["state"] == "queued"
    assert not workspace.fabric.requests


def test_missing_evidence_and_write_only_receipt_do_not_disclose_outputs(workspace):
    task = submit(workspace, task_body(upload(workspace)))
    workspace.compute.tick()
    workspace.fabric.tasks[task["task_id"]].update(status="done", evidence={"exit_code": 0})
    workspace.compute._reconcile(workspace.compute.store.get("team", task["task_id"], private=True))
    result = workspace.compute.store.get("team", task["task_id"])
    assert result["state"] == "failed" and result["error"] == "workspace_evidence_missing"
    writer = replace(workspace.user, scopes=frozenset({"agent.tasks:write"}))
    response = submit(workspace, task_body(upload(workspace)), user=writer)
    assert "workspace_result" not in response
    assert "workspace" not in response


def test_evidence_retry_reuses_already_archived_output(workspace, vault, monkeypatch):
    from pbatch.hub_vault import VaultArchiveError
    task = submit(workspace, task_body(upload(workspace)))
    workspace.compute.tick()
    private = finish(workspace, task["task_id"])
    original = workspace.connection.archive.archive
    calls = []

    def archive(task_id, evidence):
        calls.append(deepcopy(evidence))
        if len(calls) == 1:
            raise VaultArchiveError("transient", code="transport")
        return original(task_id, evidence)

    monkeypatch.setattr(workspace.connection.archive, "archive", archive)
    workspace.compute._reconcile(private)
    private = workspace.compute.store.get("team", task["task_id"], private=True)
    assert private["archive_state"] == "failed" and private["workspace_result"]["state"] == "ready"
    assert private["_workspace_output"] is None
    output_ref = private["workspace_result"]["artifact_ref"]
    workspace.compute._reconcile(private)
    result = workspace.compute.store.get("team", task["task_id"])
    assert result["archive_state"] == "archived" and result["workspace_result"]["artifact_ref"] == output_ref
    assert calls[0] == calls[1]
    assert len(vault[1].objects[output_ref["key"]]) == 1


@pytest.mark.parametrize("remote,state", [("cancelled", "cancelled"), ("failed", "failed"), ("lost", "lost")])
def test_incomplete_outputs_preserve_unsuccessful_terminal_state(workspace, remote, state):
    task = submit(workspace, task_body(upload(workspace)))
    workspace.compute.tick()
    empty = bundle({})
    private = workspace.compute.store.get("team", task["task_id"], private=True)
    workspace.fabric.tasks[task["task_id"]].update(status=remote, evidence={"exit_code": 1, "workspace": {
        "input_sha256": private["workspace_result"]["input_sha256"], "bundle": empty,
        "output_sha256": bundle_digest(empty)}})
    workspace.compute._reconcile(private)
    result = workspace.compute.store.get("team", task["task_id"])
    assert result["state"] == state and result["workspace_result"]["state"] == "unavailable"


def test_snapshot_admission_quota_counts_pending_uploads(workspace):
    store = workspace.compute.workspace.store
    content = bundle({})
    for index in range(200):
        store.reserve(workspace.user, workspace.session, f"pending-{index}",
                      bundle_digest(content), 43, 0)
    with pytest.raises(HubStoreError) as error:
        store.reserve(workspace.user, workspace.session, "overflow", bundle_digest(content), 43, 0)
    assert error.value.status == 429
    other = replace(workspace.user, subject="bob")
    assert store.reserve(other, workspace.session, "other-actor", bundle_digest(content), 43, 0)
