"""Real Hub HTTP and Unix capability transport; no provider or external account."""

import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from pbatch.hub_auth import GATEWAY_SCOPE, USER_SCOPES, HubAuth
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_compute import HubCompute
from pbatch.hub_compute_broker import broker_request, turn_tools
from pbatch.hub_compute_client import ComputeClient
from pbatch.hub_compute_config import ComputeConnection, connections
from pbatch.hub_compute_models import COMPUTE_SCOPES
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore


def grant(token, kind="user", scopes=USER_SCOPES, tenant="team"):
    return {"subject": token, "client_id": token, "tenant_id": tenant, "kind": kind,
            "instances": ["source"], "compute_instances": ["remote"], "scopes": list(scopes),
            "token_sha256": hashlib.sha256(token.encode()).hexdigest()}


@pytest.fixture
def transport(tmp_path):
    hub = HubStore(tmp_path / "hub.sqlite")
    binding = {"device-b": {"instance_id": "remote", "workspaces": {"project": "/work"}}}
    connection = SimpleNamespace(bindings=binding, archive=None)
    compute = HubCompute(hub, configured={"team": connection})
    auth = HubAuth({"mode": "development", "grants": [
        grant("user"), grant("outsider", tenant="other"),
        grant("reader", scopes={"agent.sessions:read", "agent.tasks:read"}),
        grant("gateway", "gateway", COMPUTE_SCOPES | {GATEWAY_SCOPE})]})
    server = HubHTTPServer(("127.0.0.1", 0), hub, auth, compute=compute)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    gateway = HubClient(url, "gateway")
    gateway.register_instance(instance_id="source", boot_id="boot", name="source", project_id="project")
    session = gateway.register_session(instance_id="source", generation=1,
                                        local_session_id="local", project_id="project",
                                        name="work", status="idle", controllable=True)
    yield SimpleNamespace(hub=hub, compute=compute, url=url, gateway=gateway,
                           user=HubClient(url, "user"), session=session["session_id"])
    server.shutdown()
    server.server_close()
    thread.join(5)
    hub.close()


def test_http_compute_scopes_tenant_pagination_and_idempotency(transport):
    client = ComputeClient(transport.user)
    task = client.submit(transport.session, {"argv": ["python", "test.py"]}, "same-key")
    assert client.submit(transport.session, {"argv": ["python", "test.py"]}, "same-key") == task
    assert client.tasks(transport.session) == [task]
    assert client.get(task["task_id"]) == task
    with pytest.raises(HubError) as conflict:
        client.submit(transport.session, {"argv": ["false"]}, "same-key")
    assert conflict.value.status == 409
    outsider = ComputeClient(HubClient(transport.url, "outsider"))
    assert outsider.tasks() == []
    with pytest.raises(HubError) as missing:
        outsider.get(task["task_id"])
    assert missing.value.status == 404
    reader = ComputeClient(HubClient(transport.url, "reader"))
    assert reader.tasks() == [task]
    with pytest.raises(HubError) as forbidden:
        reader.cancel(task["task_id"])
    assert forbidden.value.status == 403
    assert client.cancel(task["task_id"])["state"] == "cancelled"


def test_http_reschedule_queued_task_and_reject_running_task(transport):
    client = ComputeClient(transport.user)
    task = client.submit(transport.session, {"argv": ["python", "test.py"]}, "retarget-key")
    moved = client.reschedule(task["task_id"], "device-b")
    assert moved["target_device_id"] == "device-b"
    assert moved["state"] == "queued"
    assert client.reschedule(task["task_id"], "device-b") == moved
    transport.compute.store.update("team", task["task_id"], {}, state="running")
    with pytest.raises(HubError) as error:
        client.reschedule(task["task_id"], "")
    assert error.value.status == 409


def test_http_lost_retry_requires_confirmation_and_uses_new_queue_admission(transport):
    client = ComputeClient(transport.user)
    task = client.submit(transport.session, {
        "argv": ["python", "test.py"], "target_device_id": "device-b"},
        "lost-retry-key")
    transport.compute.store.update("team", task["task_id"], {}, state="lost")
    with pytest.raises(ValueError, match="confirm_duplicate"):
        client.retry(task["task_id"])
    retried = client.retry(task["task_id"], confirm_duplicate=True)
    assert retried["state"] == "queued"
    assert retried["target_device_id"] == "device-b"
    private = transport.compute.store.get("team", task["task_id"], private=True)
    assert private["_fabric_request_id"].endswith(":retry-1")


def test_local_capability_is_bound_to_running_turn_and_revoked_on_exit(transport):
    turn = transport.user.send_prompt(transport.session, "run compute", "turn-key")["turn_id"]
    transport.hub.append_event("team", "source", 1, {
        "event_id": "running", "session_id": transport.session, "turn_id": turn,
        "kind": "turn.running", "payload": {}})
    gateway = SimpleNamespace(options=SimpleNamespace(compute_tools=True),
                              instance_id="source", generation=1, client=transport.gateway)
    managed = SimpleNamespace(hub_id=transport.session)
    with turn_tools(gateway, managed, turn, "original prompt") as prompt:
        import shlex
        line = next(line for line in prompt.splitlines() if "--via" in line)
        path = shlex.split(line)[shlex.split(line).index("--via") + 1]
        assert prompt.endswith("original prompt")
        task = broker_request(path, "submit", {"argv": ["python", "task.py"]}, "tool-key")
        assert task["turn_id"] == turn and task["actor"] == "user"
        assert broker_request(path, "status", {"task_id": task["task_id"]})["state"] == "queued"
        transport.hub.cancel_turn("team", turn, "user")
        with pytest.raises(HubError, match="active running turn"):
            broker_request(path, "submit", {"argv": ["true"]}, "late-key")
    with pytest.raises((OSError, ValueError)):
        broker_request(path, "status", {"task_id": task["task_id"]})


def settings(tmp_path):
    credential = tmp_path / "credential.json"
    credential.write_text(json.dumps({"device_id": "service", "secret": "secret-one"}))
    credential.chmod(0o600)
    return {"tenant_id": "team", "base_url": "http://127.0.0.1:9999", "credential_file": str(credential),
            "bindings": [{"device_id": "worker", "instance_id": "remote", "workspaces": {"project": "/work"}}]}


def test_operator_bindings_rotation_and_separate_tenant_planes(tmp_path):
    configured = settings(tmp_path)
    connection = ComputeConnection(configured)
    assert connection.client().secret == "secret-one"
    from pathlib import Path
    credential = Path(configured["credential_file"])
    credential.write_text(json.dumps({"device_id": "service", "secret": "secret-two"}))
    assert connection.client().secret == "secret-two"
    with pytest.raises(ValueError, match="distinct"):
        connections([configured, dict(configured, tenant_id="other")])
    with pytest.raises(ValueError, match="loopback"):
        ComputeConnection(dict(configured, base_url="http://example.test"))
    credential.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        connection.client()


def test_same_tenant_can_federate_distinct_fabric_endpoints(tmp_path):
    first = settings(tmp_path)
    second = settings(tmp_path)
    second.update(base_url="http://127.0.0.1:9998", bindings=[{
        "device_id": "worker-two", "instance_id": "remote-two",
        "workspaces": {"project": "/work-two"}}])
    configured = connections([first, second])
    assert [item.url for item in configured["team"]] == [first["base_url"], second["base_url"]]
    overlap = dict(second, bindings=[dict(second["bindings"][0], device_id="worker")])
    with pytest.raises(ValueError, match="share a device"):
        connections([first, overlap])


@pytest.mark.parametrize("workspace", ["relative", "/work/../escape", "C:\\work"])
def test_workspace_bindings_are_explicit_absolute_roots(tmp_path, workspace):
    configured = settings(tmp_path)
    configured["bindings"][0]["workspaces"]["project"] = workspace
    with pytest.raises(ValueError):
        ComputeConnection(configured)
