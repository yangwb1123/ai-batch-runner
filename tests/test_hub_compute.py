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
        self.fail_inventory = False

    def inventory(self, ids):
        if self.fail_inventory:
            return {"error": "fabric offline", "devices": []}
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


def test_prestart_remote_loss_requeues_for_other_device_without_duplicate(system):
    """A lost lease before Runner start is safe to place again."""
    original_submit = system.fabric.submit_task

    def submit_queued(argv, **options):
        result = original_submit(argv, **options)
        task_id = options["request_id"]
        system.fabric.tasks[task_id]["status"] = "queued"
        result["task"]["status"] = "queued"
        return result

    system.fabric.submit_task = submit_queued
    task = submit(system, key="prestart-loss")
    system.compute.tick()
    task_id = task["task_id"]
    assert system.compute.store.get("team", task_id)["state"] == "dispatching"

    system.fabric.tasks[task_id].update(status="lost", started_at=None)
    system.compute._reconcile(system.compute.store.get("team", task_id, private=True))
    recovered = system.compute.store.get("team", task_id, private=True)
    assert recovered["state"] == "queued"
    assert recovered["device_id"] == ""
    assert recovered["_fabric_id"] == ""
    assert recovered["error"] == "device_lost_before_start"
    assert recovered["_fabric_request_id"] == f"{task_id}:attempt-1"
    events = system.hub.events("team", system.session["session_id"], 0, 50)
    assert events[-1]["kind"] == "compute.queued"
    assert events[-1]["payload"]["reason"] == "device_lost_before_start"

    system.compute._reconcile(recovered)
    assert len(system.fabric.requests) == 2
    assert system.fabric.requests[1]["request_id"] == f"{task_id}:attempt-1"


def test_started_remote_loss_stays_terminal(system):
    """Start evidence keeps irreversible work terminal instead of retrying."""
    task = submit(system, key="started-loss")
    system.compute.tick()
    task_id = task["task_id"]
    system.fabric.tasks[task_id].update(status="lost", started_at=123.0)
    system.compute._reconcile(system.compute.store.get("team", task_id, private=True))
    assert system.compute.store.get("team", task_id)["state"] == "lost"


def test_federated_tenant_aggregates_inventory_and_routes_to_bound_fabric(tmp_path):
    """One tenant can expose devices from several Fabric control planes."""
    hub = HubStore(tmp_path / "federated.sqlite")
    hub.register_instance("team", "source", {"boot_id": "boot"})
    session = hub.upsert_session("team", "source", 1, {
        "local_session_id": "local", "name": "session", "project_id": "project",
        "status": "idle", "controllable": True})
    first, second = Fabric(), Fabric()
    second.device_id = "hub-service-two"
    one = SimpleNamespace(client=lambda: first,
                          bindings={"device-a": {"instance_id": "remote-a",
                                                 "workspaces": {"project": "/one"}}}, archive=None)
    two = SimpleNamespace(client=lambda: second,
                          bindings={"device-b": {"instance_id": "remote-b",
                                                 "workspaces": {"project": "/two"}}}, archive=None)
    compute = HubCompute(hub, configured={"team": [one, two]})
    principal = HubPrincipal("team", "alice", "console", "user", frozenset({"source"}),
                             USER_SCOPES, frozenset({"remote-a", "remote-b"}))
    service = HubService(hub, compute)
    try:
        devices = service.dispatch("GET", PREFIX + "/devices", {"limit": "20"}, {}, principal)[1]["data"]
        assert {row["device_id"] for row in devices} == {"device-a", "device-b"}
        task = service.dispatch(
            "POST", PREFIX + "/sessions/" + session["session_id"] + "/tasks", {},
            {"argv": ["python", "job.py"], "target_device_id": "device-b"},
            principal, "federated-task")[1]["data"]
        compute.tick()
        stored = compute.store.get("team", task["task_id"])
        assert stored["state"] == "running" and stored["device_id"] == "device-b"
        assert not first.requests
        assert second.requests[0]["cwd"] == "/two"
        assert second.requests[0]["target_device"] == "device-b"
        first.online = False
        automatic = service.dispatch(
            "POST", PREFIX + "/sessions/" + session["session_id"] + "/tasks", {},
            {"argv": ["python", "automatic.py"]}, principal, "federated-auto")[1]["data"]
        compute.tick()
        selected = compute.store.get("team", automatic["task_id"])
        assert selected["device_id"] == "device-b"
        assert len(second.requests) == 2
        first.fail_inventory = True
        with pytest.raises(HubAccessError, match="inventory unavailable"):
            compute.inventory("team")
        pinned_unavailable = service.dispatch(
            "POST", PREFIX + "/sessions/" + session["session_id"] + "/tasks", {},
            {"argv": ["python", "pinned.py"], "target_device_id": "device-a"},
            principal, "federated-pinned-unavailable")[1]["data"]
        compute.tick()
        waiting = compute.store.get("team", pinned_unavailable["task_id"])
        assert waiting["state"] == "queued"
        assert waiting["error"] == "waiting_for_eligible_device"
        degraded = service.dispatch(
            "POST", PREFIX + "/sessions/" + session["session_id"] + "/tasks", {},
            {"argv": ["python", "degraded.py"]}, principal, "federated-degraded")[1]["data"]
        compute.tick()
        assert compute.store.get("team", degraded["task_id"])["device_id"] == "device-b"
        assert len(second.requests) == 3
    finally:
        hub.close()


def test_missing_remote_start_field_stays_terminal(system):
    """A malformed loss response fails closed instead of being retried."""
    original_submit = system.fabric.submit_task

    def submit_queued(argv, **options):
        result = original_submit(argv, **options)
        task_id = options["request_id"]
        system.fabric.tasks[task_id]["status"] = "queued"
        result["task"]["status"] = "queued"
        return result

    system.fabric.submit_task = submit_queued
    task = submit(system, key="missing-start-field")
    system.compute.tick()
    task_id = task["task_id"]
    system.fabric.tasks[task_id].update(status="lost")
    system.compute._reconcile(system.compute.store.get("team", task_id, private=True))
    assert system.compute.store.get("team", task_id)["state"] == "lost"


def test_task_actor_can_retarget_queued_work_and_worker_uses_new_target(system):
    user = replace(system.user, compute_instances=frozenset({"source", "remote"}))
    system.fabric.online = False
    task = submit(system, {"argv": ["python", "job.py"]}, key="retarget", user=user)
    path = PREFIX + "/tasks/" + task["task_id"] + "/reschedule"
    _, response = system.service.dispatch(
        "POST", path, {}, {"target_device_id": "device-a"}, user)
    assert response["data"]["target_device_id"] == "device-a"
    assert response["data"]["device_id"] == ""
    event = system.hub.events("team", system.session["session_id"], 0, 20)[-1]
    assert event["kind"] == "compute.queued"
    assert event["payload"]["reason"] == "rescheduled"
    assert event["payload"]["target_device_id"] == "device-a"
    system.fabric.online = True
    system.compute.tick()
    result = system.compute.store.get("team", task["task_id"])
    assert result["state"] == "dispatching" and result["device_id"] == "device-a"


def test_reschedule_is_owner_scoped_authorized_and_queued_only(system):
    user = replace(system.user, compute_instances=frozenset({"source", "remote"}))
    task = submit(system, key="owner", user=user)
    path = PREFIX + "/tasks/" + task["task_id"] + "/reschedule"
    other = replace(user, subject="bob")
    with pytest.raises(HubAccessError, match="task actor"):
        system.service.dispatch("POST", path, {}, {"target_device_id": "device-a"}, other)
    with pytest.raises(ValueError, match="idempotency"):
        system.service.dispatch("POST", path, {}, {"target_device_id": "device-b"}, user,
                                 "unexpected-key")
    with pytest.raises(ValueError, match="target_device_id"):
        system.service.dispatch("POST", path, {}, {}, user)
    system.compute.store.update("team", task["task_id"], {}, state="running")
    with pytest.raises(HubStoreError, match="queued"):
        system.service.dispatch("POST", path, {}, {"target_device_id": ""}, user)

    limited = replace(system.user, compute_instances=frozenset({"remote"}))
    original_only = submit(system, key="original-target", user=limited)
    original_path = PREFIX + "/tasks/" + original_only["task_id"] + "/reschedule"
    expanded = replace(limited, compute_instances=frozenset({"source", "remote"}))
    with pytest.raises(HubAccessError, match="authorized"):
        system.service.dispatch(
            "POST", original_path, {}, {"target_device_id": "device-a"}, expanded)


def test_lost_task_requires_explicit_duplicate_confirmation_and_requeues(system):
    task = submit(system, key="explicit-lost-retry")
    system.compute.tick()
    task_id = task["task_id"]
    system.fabric.tasks[task_id].update(status="lost", started_at=123.0)
    system.compute._reconcile(system.compute.store.get("team", task_id, private=True))
    assert system.compute.store.get("team", task_id)["state"] == "lost"
    path = PREFIX + "/tasks/" + task_id + "/retry"
    with pytest.raises(ValueError, match="confirm_duplicate"):
        system.service.dispatch("POST", path, {}, {"target_device_id": ""}, system.user)

    status, response = system.service.dispatch(
        "POST", path, {}, {"confirm_duplicate": True}, system.user)
    assert status == 202
    retried = response["data"]
    assert retried["state"] == "queued"
    assert retried["target_device_id"] == ""
    private = system.compute.store.get("team", task_id, private=True)
    assert private["_fabric_request_id"] == f"{task_id}:retry-1"
    assert private["_fabric_id"] == ""
    assert private["error"] == "retry_requested"
    events = system.hub.events("team", system.session["session_id"], 0, 50)
    assert events[-1]["payload"]["reason"] == "retry_requested"
    system.compute._reconcile(private)
    assert len(system.fabric.requests) == 2
    assert system.fabric.requests[-1]["request_id"] == f"{task_id}:retry-1"


def test_lost_retry_preserves_original_explicit_target(system):
    user = replace(system.user, compute_instances=frozenset({"remote"}))
    task = submit(system, {"argv": ["python", "job.py"], "target_device_id": "device-b"},
                  key="explicit-target-lost", user=user)
    system.compute.tick()
    task_id = task["task_id"]
    system.fabric.tasks[task_id].update(status="lost", started_at=123.0)
    system.compute._reconcile(system.compute.store.get("team", task_id, private=True))
    path = PREFIX + "/tasks/" + task_id + "/retry"
    _, response = system.service.dispatch(
        "POST", path, {}, {"confirm_duplicate": True}, user)
    assert response["data"]["target_device_id"] == "device-b"


def test_lost_retry_rejects_tasks_with_archive_evidence_pending(system):
    task = submit(system, key="archived-lost-retry")
    system.compute.store.update("team", task["task_id"], {"archive_state": "pending"},
                                state="lost")
    path = PREFIX + "/tasks/" + task["task_id"] + "/retry"
    with pytest.raises(HubStoreError, match="archived"):
        system.service.dispatch("POST", path, {}, {"confirm_duplicate": True}, system.user)


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
