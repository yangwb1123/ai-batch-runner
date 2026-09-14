"""Durable scheduling, least-authority delegation and failure recovery."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pbatch.hub_auth import GATEWAY_SCOPE, HubPrincipal, USER_SCOPES
from pbatch.hub_compute import HubCompute
from pbatch.hub_compute_models import COMPUTE_SCOPES, request_body
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import HubService, PREFIX
from pbatch.hub_store import HubStore, HubStoreError


class Fabric:
    def __init__(self):
        self.device_id = "hub-service"
        self.tasks = {}
        self.requests = []
        self.drop_ack = False
        self.online = True

    def inventory(self, ids):
        return {"capabilities": ["hub.compute.v1"], "devices": [
            {"device_id": device, "online": self.online, "lifecycle_state": "ready",
             "capability_valid_until": "2099-01-01T00:00:00Z",
             "static_capabilities": {"os": "Linux", "architecture": "x86_64"},
             "dynamic_state": {"available_memory_bytes": 10000000},
             "resources": {"cpu_cores": 8, "memory_bytes": 10000000,
                           "reserved_cpu_cores": 0, "reserved_memory_bytes": 0},
             "runtimes": {"python": "3"}} for device in ids]}

    def submit_task(self, argv, **options):
        self.requests.append(options)
        key = options["request_id"]
        self.tasks.setdefault(key, {"task_id": key, "status": "running"})
        if self.drop_ack:
            self.drop_ack = False
            return {"error": "ack lost"}
        return {"task": dict(self.tasks[key])}

    def task_status(self, task_id):
        return {"task": dict(self.tasks[task_id])}

    def task_cancel(self, task_id):
        self.tasks[task_id]["status"] = "cancelled"
        return {"ok": True}


@pytest.fixture
def system(tmp_path):
    hub = HubStore(tmp_path / "hub.sqlite")
    hub.register_instance("team", "source", {"boot_id": "boot"})
    session = hub.upsert_session("team", "source", 1, {
        "local_session_id": "local", "name": "session", "project_id": "project",
        "status": "idle", "controllable": True})
    fabric = Fabric()
    bindings = {device: {"instance_id": instance, "workspaces": {"project": "/workspace"}}
                for device, instance in (("device-a", "source"), ("device-b", "remote"))}
    connection = SimpleNamespace(client=lambda: fabric, bindings=bindings, archive=None)
    compute = HubCompute(hub, configured={"team": connection})
    user = HubPrincipal("team", "alice", "console", "user", frozenset({"source"}),
                        USER_SCOPES, frozenset({"remote"}))
    value = SimpleNamespace(hub=hub, fabric=fabric, connection=connection, compute=compute,
                            user=user, session=session, service=HubService(hub, compute))
    yield value
    hub.close()


def submit(system, body=None, key="key", user=None):
    return system.service.dispatch("POST", PREFIX + "/sessions/" + system.session["session_id"] + "/tasks",
                                   {}, body or {"argv": ["python", "job.py"]}, user or system.user, key)[1]["data"]


def test_concurrent_idempotency_authorized_placement_and_private_projection(system):
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: submit(system), range(8)))
    assert len({task["task_id"] for task in results}) == 1
    with pytest.raises(HubStoreError, match="different content"):
        submit(system, {"argv": ["python", "different.py"]})
    system.compute.tick()
    task = system.compute.store.get("team", results[0]["task_id"])
    assert task["state"] == "running"
    assert task["device_id"] == "device-b"
    assert not any(key.startswith("_") for key in task)
    assert "/workspace" not in json.dumps(task)
    assert system.fabric.requests[0]["mobility"] == "pinned"
    assert system.fabric.requests[0]["cwd"] == "/workspace"
    assert system.fabric.requests[0]["hub_context"]["actor"] == "alice"
    with pytest.raises(HubAccessError):
        submit(system, {"argv": ["true"], "target_device_id": "device-a"}, key="forbidden")


def test_lost_ack_restart_keeps_exact_request_and_cancel(system):
    system.fabric.drop_ack = True
    task = submit(system)
    system.compute.tick()
    task_id = task["task_id"]
    assert system.compute.store.get("team", task_id)["state"] == "dispatching"
    path = system.hub.path
    system.hub.close()

    system.hub = HubStore(path)
    recovered = HubCompute(system.hub, configured={"team": system.connection})
    recovered.store.cancel("team", task_id)
    recovered.tick()
    assert recovered.store.get("team", task_id)["state"] == "cancelled"
    assert len(system.fabric.tasks) == 1
    assert system.fabric.requests[0] == system.fabric.requests[1]
    system.hub.close()


def test_service_identity_change_does_not_resubmit_in_a_new_namespace(system):
    system.fabric.drop_ack = True
    task = submit(system)
    system.compute.tick()
    system.fabric.device_id = "replacement-service"
    private = system.compute.store.get("team", task["task_id"], private=True)
    system.compute._attempt(private)
    result = system.compute.store.get("team", task["task_id"])
    assert result["state"] == "dispatching"
    assert result["error"] == "fabric_identity_changed"
    assert len(system.fabric.requests) == 1

def test_queue_wait_cancel_and_result_redaction(system):
    system.fabric.online = False
    queued = submit(system)
    system.compute.tick()
    assert system.compute.store.get("team", queued["task_id"])["state"] == "queued"
    assert system.compute.store.cancel("team", queued["task_id"])["state"] == "cancelled"
    assert not system.fabric.requests
    system.fabric.online = True
    task = submit(system, key="second")
    system.compute.tick()
    task_id = task["task_id"]
    system.fabric.tasks[task_id].update(status="done", evidence={
        "exit_code": 0, "stdout": "x" * 10000, "stderr": "", "elapsed_seconds": 1.5},
        evidence_digest="accepted-digest")
    private = system.compute.store.get("team", task_id, private=True)
    system.compute._reconcile(private)
    complete = system.compute.store.get("team", task_id)
    assert complete["state"] == "completed"
    assert len(complete["result"]["stdout"]) == 8192
    assert complete["result"]["output_truncated"]
    assert complete["result"]["evidence_digest"] == "accepted-digest"
    assert system.compute.store.cancel("team", task_id)["state"] == "completed"


def _running_turn(system, user=None):
    _, result = system.service.dispatch("POST", PREFIX + "/sessions/" + system.session["session_id"] + "/turns",
                                       {}, {"prompt": "compute"}, user or system.user, "prompt-key")
    turn = result["data"]["turn_id"]
    system.hub.append_event("team", "source", 1, {
        "event_id": "running", "session_id": system.session["session_id"],
        "turn_id": turn, "kind": "turn.running", "payload": {}})
    return turn


def _delegate(system, turn, operation, payload=None, gateway=None):
    gateway = gateway or HubPrincipal("team", "gateway", "cli", "gateway", frozenset({"source"}),
                                     COMPUTE_SCOPES | {GATEWAY_SCOPE}, frozenset({"source", "remote"}))
    return system.service.dispatch("POST", PREFIX + "/gateway/compute", {}, {
        "instance_id": "source", "generation": 1, "session_id": system.session["session_id"],
        "turn_id": turn, "operation": operation, "payload": payload or {}, "key": "agent-key"}, gateway)[1]["data"]


def test_agent_intersection_and_turn_expiration(system):
    turn = _running_turn(system)
    devices = _delegate(system, turn, "devices")
    assert [row["device_id"] for row in devices] == ["device-b"]
    task = _delegate(system, turn, "submit", {"argv": ["python", "job.py"]})
    assert task["actor"] == "alice" and task["turn_id"] == turn
    assert _delegate(system, turn, "status", {"task_id": task["task_id"]})["state"] == "queued"
    system.hub.cancel_turn("team", turn, "alice")
    with pytest.raises(HubAccessError, match="active running turn"):
        _delegate(system, turn, "submit", {"argv": ["true"]})


def test_prompt_permission_cannot_borrow_gateway_compute_authority(system):
    limited = replace(system.user, scopes=frozenset({"agent.sessions:write"}))
    turn = _running_turn(system, limited)
    with pytest.raises(HubAccessError, match="scope"):
        _delegate(system, turn, "devices")
    with pytest.raises(HubAccessError, match="scope"):
        _delegate(system, turn, "submit", {"argv": ["true"]})


def test_user_cannot_attach_compute_to_another_actors_turn(system):
    turn = _running_turn(system)
    other = replace(system.user, subject="bob")
    with pytest.raises(HubAccessError, match="actor and session"):
        submit(system, {"argv": ["true"], "turn_id": turn}, user=other)


def test_write_and_cancel_receipts_do_not_disclose_task_outputs(system):
    writer = replace(system.user, scopes=frozenset({"agent.tasks:write"}))
    task = submit(system, user=writer)
    system.compute.store.update("team", task["task_id"],
                                {"result": {"stdout": "sensitive output"}}, state="completed")
    replay = submit(system, user=writer)
    assert replay["result"] == {} and replay["argv"] == ["[redacted]"]
    canceller = replace(system.user, scopes=frozenset({"agent.tasks:cancel"}))
    _, response = system.service.dispatch("POST", PREFIX + "/tasks/" + task["task_id"] + "/cancel",
                                          {}, {}, canceller)
    assert response["data"]["result"] == {} and response["data"]["argv"] == ["[redacted]"]


def test_one_actor_cannot_exhaust_shared_compute_admission(system):
    for index in range(32):
        submit(system, key=f"queued-{index}")
    with pytest.raises(HubStoreError) as full:
        submit(system, key="overflow")
    assert full.value.status == 429
    other = replace(system.user, subject="bob")
    assert submit(system, key="other-actor", user=other)["state"] == "queued"


def test_slow_archive_does_not_block_execution_or_cancellation(system):
    entered, release = threading.Event(), threading.Event()

    def archive(task_id, evidence):
        entered.set()
        assert release.wait(10)
        return {"backend": "aero-vault", "key": task_id, "version_id": "version"}

    system.connection.archive = SimpleNamespace(archive=archive)
    first = submit(system)
    system.compute.store.update("team", first["task_id"], {}, state="completed", delay=0)
    system.compute.start()
    try:
        assert entered.wait(3)
        task = submit(system, key="during-archive")
        _wait_state(system.compute, task["task_id"], "running")
        system.compute.store.cancel("team", task["task_id"])
        _wait_state(system.compute, task["task_id"], "cancelled")
        assert not release.is_set()
    finally:
        release.set()
        system.compute.close()


def _wait_state(compute, task_id, state):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if compute.store.get("team", task_id)["state"] == state:
            return
        time.sleep(0.02)
    raise AssertionError("compute did not reach " + state)


@pytest.mark.parametrize("payload", [
    {"argv": "sh command"}, {"argv": ["true"], "timeout": True},
    {"argv": ["true"], "workdir": "../../escape"}, {"argv": ["true"], "workdir": "/tmp"},
    {"argv": ["true"], "resources": {"memory_bytes": -1}},
    {"argv": ["true"], "requirements": {"runtimes": "python"}},
    {"argv": ["true"], "env": {"SECRET": "x"}}, {"argv": ["a" * 20000]},
])
def test_invalid_execution_requests_fail_closed(payload):
    with pytest.raises(ValueError):
        request_body(payload)
