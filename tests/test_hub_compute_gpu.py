"""GPU requests retain CPU compatibility and cannot bypass authorized placement.

Inventory replacements below are read-only device-probe test doubles. ComputeStore
owns the durable task history and lifecycle events used by the recovery assertions.
"""

from copy import deepcopy
from dataclasses import replace
import json
import time

import pytest

from pbatch.hub_compute import HubCompute
from pbatch.hub_compute_cli import _submission, parser
from pbatch.hub_compute_gpu import gpu_inventory
from pbatch.hub_compute_models import request_body
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import PREFIX
from pbatch.hub_store import HubStore, HubStoreError
from test_hub_compute import _delegate, _running_turn, submit, system  # noqa: F401

GPU_A = "GPU-00000000-0000-0000-0000-000000000001"
GPU_B = "GPU-00000000-0000-0000-0000-000000000002"
MIB = 1024**2


def card(uuid=GPU_A, memory=8192 * MIB):
    return {"uuid": uuid, "name": "Test CUDA GPU", "vendor": "nvidia",
            "total_memory_bytes": 16384 * MIB, "available_memory_bytes": memory,
            "schedulable": True, "reserved": False, "observed_at": time.time()}


def gpu_request(count=1, memory=4096 * MIB):
    return {"argv": ["python", "job.py"],
            "resources": {"gpu_count": count, "gpu_memory_bytes": memory}}


@pytest.fixture
def gpu_system(system):
    original_inventory = system.fabric.inventory
    original_submit = system.fabric.submit_task

    def inventory(ids):
        result = original_inventory(ids)
        result["capabilities"].append("hub.compute.gpu.v1")
        for row in result["devices"]:
            row.update(gpu_status="available", gpus=[card(), card(GPU_B)])
        return result

    def dispatch(argv, **options):
        result = original_submit(argv, **options)
        resources = options["resources"]
        if resources.get("gpu_count"):
            now = time.time()
            system.fabric.tasks[options["request_id"]].setdefault("gpu_assignment", {
                "vendor": "nvidia", "mode": "physical", "uuids": [GPU_A, GPU_B][:resources["gpu_count"]],
                "gpu_memory_bytes": resources["gpu_memory_bytes"],
                "assigned_at": now, "observed_at": now})
            if "task" in result:
                result["task"] = deepcopy(system.fabric.tasks[options["request_id"]])
        return result

    system.fabric.inventory = inventory
    system.fabric.submit_task = dispatch
    return system


@pytest.mark.parametrize("resources", [
    {"gpu_count": True}, {"gpu_count": 1.0}, {"gpu_count": "1"}, {"gpu_count": -1},
    {"gpu_count": 17}, {"gpu_count": None}, {"gpu_memory_bytes": 1},
    {"gpu_count": 1, "gpu_memory_bytes": -1}, {"gpu_count": 1, "gpu_memory_bytes": True},
    {"gpu_count": 1, "gpu_memory_bytes": float("nan")},
    {"gpu_count": 1, "gpu_memory_bytes": 1024**4 + 1},
    {"gpu_count": 1, "gpu_uuids": [GPU_A]},
])
def test_gpu_request_bounds(resources):
    with pytest.raises(ValueError):
        request_body({"argv": ["true"], "resources": resources})


def test_zero_gpu_defaults_preserve_cpu_request_and_existing_idempotency(system):
    original = {"argv": ["python", "job.py"]}
    explicit_zero = dict(original, resources={"gpu_count": 0, "gpu_memory_bytes": 0})
    assert request_body(original) == request_body(explicit_zero)
    assert submit(system, original)["task_id"] == submit(system, explicit_zero)["task_id"]
    assert set(request_body(original)["resources"]) == {"cpu_cores", "memory_bytes"}


def test_gpu_cli_uses_per_card_mib_and_validates_before_submit():
    args = parser().parse_args(["submit", "--argv-json", '["python","job.py"]',
                                "--gpu-count", "2", "--gpu-memory-mib", "6144"])
    assert _submission(args)["resources"] == {
        "cpu_cores": 1, "memory_bytes": 0, "gpu_count": 2, "gpu_memory_bytes": 6144 * MIB}
    args.gpu_count = 0
    with pytest.raises(ValueError, match="positive gpu_count"):
        _submission(args)


def test_gpu_placement_obeys_instance_authority_and_persists_assignment(gpu_system):
    value = gpu_system
    task = submit(value, gpu_request(2))
    with pytest.raises(HubStoreError, match="different content"):
        submit(value, gpu_request(1))
    value.compute.tick()
    result = value.compute.store.get("team", task["task_id"])
    assert result["state"] == "running" and result["device_id"] == "device-b"
    assert result["gpu_assignment"]["uuids"] == [GPU_A, GPU_B]
    assert value.fabric.requests[0]["resources"]["gpu_count"] == 2
    _, response = value.service.dispatch("GET", PREFIX + "/devices", {}, {}, value.user)
    assert [row["device_id"] for row in response["data"]] == ["device-b"]
    assert response["data"][0]["gpus"][0]["uuid"] == GPU_A


@pytest.mark.parametrize("memory,count", [(9000 * MIB, 1), (4096 * MIB, 3)])
def test_per_card_memory_or_count_shortage_stays_queued(gpu_system, memory, count):
    task = submit(gpu_system, gpu_request(count, memory))
    gpu_system.compute.tick()
    assert gpu_system.compute.store.get("team", task["task_id"])["state"] == "queued"
    assert not gpu_system.fabric.requests


def test_old_fabric_gpu_claims_do_not_enable_gpu_but_cpu_still_runs(gpu_system):
    inventory = gpu_system.fabric.inventory

    def old_inventory(ids):
        response = inventory(ids)
        response["capabilities"] = ["hub.compute.v1"]
        return response

    gpu_system.fabric.inventory = old_inventory
    gpu_task = submit(gpu_system, gpu_request())
    cpu_task = submit(gpu_system, key="cpu")
    gpu_system.compute.tick()
    assert gpu_system.compute.store.get("team", gpu_task["task_id"])["state"] == "queued"
    assert gpu_system.compute.store.get("team", cpu_task["task_id"])["state"] == "running"
    assert len(gpu_system.fabric.requests) == 1


@pytest.mark.parametrize("change", [
    {"available_memory_bytes": float("nan")}, {"total_memory_bytes": 0},
    {"uuid": "GPU-short"}, {"observed_at": float("inf")}, {"vendor": "other"},
    {"schedulable": "true"}, {"reserved": None}, {"name": "invalid\nname"},
    {"observed_at": 10**1000}, {"name": "\ud800"},
])
def test_malformed_gpu_row_is_unavailable(change):
    row = {"gpu_status": "available", "gpus": [dict(card(), **change)]}
    assert gpu_inventory(row, True, True) == {"gpu_status": "unavailable", "gpus": []}


def test_stale_future_duplicate_and_invalid_status_probes_fail_closed():
    for observed in (time.time() - 100, time.time() + 10):
        row = {"gpu_status": "available", "gpus": [dict(card(), observed_at=observed)]}
        result = gpu_inventory(row, True, True)
        assert result["gpu_status"] == "stale" and not result["gpus"][0]["schedulable"]
    for rows in ([card(), card()], [card()] * 17):
        assert gpu_inventory({"gpu_status": "available", "gpus": rows}, True, True)["gpus"] == []
    assert gpu_inventory({"gpu_status": [], "gpus": []}, True, True)["gpu_status"] == "unavailable"


def test_gpu_ack_loss_restart_preserves_assignment_and_cancellation(gpu_system):
    value = gpu_system
    value.fabric.drop_ack = True
    task = submit(value, gpu_request())
    value.compute.tick()
    original = deepcopy(value.fabric.tasks[task["task_id"]]["gpu_assignment"])
    path = value.hub.path
    value.hub.close()
    value.hub = HubStore(path)
    recovered = HubCompute(value.hub, configured={"team": value.connection})
    recovered.store.cancel("team", task["task_id"])
    recovered.tick()
    result = recovered.store.get("team", task["task_id"])
    assert result["state"] == "cancelled" and result["gpu_assignment"] == original
    assert value.fabric.requests[0] == value.fabric.requests[1]
    assert len(value.fabric.tasks) == 1


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_changed_terminal_assignment_never_becomes_success(gpu_system, missing):
    value = gpu_system
    task = submit(value, gpu_request())
    value.compute.tick()
    remote = value.fabric.tasks[task["task_id"]]
    original = remote.pop("gpu_assignment")
    if not missing:
        remote["gpu_assignment"] = dict(original, uuids=[GPU_B])
    private = value.compute.store.get("team", task["task_id"], private=True)
    remote["status"] = "done"
    value.compute._attempt(private)
    assert value.compute.store.get("team", task["task_id"])["error"] == "gpu_assignment_invalid"
    result = value.compute.store.get("team", task["task_id"])
    assert result["state"] == "failed" and result["gpu_assignment"] == original
    assert result["result"] == {}


def test_bad_initial_assignment_retains_handle_and_cancels_remote_task(gpu_system):
    value = gpu_system
    original_submit = value.fabric.submit_task

    def dispatch(argv, **options):
        result = original_submit(argv, **options)
        result["task"].pop("gpu_assignment")
        return result

    value.fabric.submit_task = dispatch
    task = submit(value, gpu_request())
    value.compute.tick()
    result = value.compute.store.get("team", task["task_id"], private=True)
    assert result["_fabric_id"] == task["task_id"]
    assert result["state"] == "cancelled" and result["error"] == "gpu_assignment_invalid"
    assert value.fabric.tasks[task["task_id"]]["status"] == "cancelled"
    assert len(value.fabric.requests) == 1


def test_bad_running_assignment_requests_cancel_and_terminal_state_converges(gpu_system):
    value = gpu_system
    task = submit(value, gpu_request())
    value.compute.tick()
    value.fabric.tasks[task["task_id"]].pop("gpu_assignment")
    for state in ("cancel_requested", "cancelled"):
        current = value.compute.store.get("team", task["task_id"], private=True)
        value.compute._reconcile(current)
        result = value.compute.store.get("team", task["task_id"])
        assert result["state"] == state and result["error"] == "gpu_assignment_invalid"


@pytest.mark.parametrize("remote_id", [["bad"], "x" * 257, "bad\nhandle"])
def test_malformed_remote_handle_retries_original_idempotent_request(gpu_system, remote_id):
    value = gpu_system
    original = value.fabric.submit_task

    def broken(*args, **kwargs):
        response = original(*args, **kwargs)
        response["task"]["task_id"] = remote_id
        return response

    value.fabric.submit_task = broken
    task = submit(value, gpu_request())
    value.compute.tick()
    current = value.compute.store.get("team", task["task_id"], private=True)
    assert current["_fabric_id"] == "" and current["_dispatch_uncertain"]
    assert len(value.fabric.tasks) == 1
    value.fabric.submit_task = original
    value.compute._reconcile(current)
    recovered = value.compute.store.get("team", task["task_id"], private=True)
    assert recovered["_fabric_id"] == task["task_id"] and recovered["state"] == "running"
    assert len(value.fabric.tasks) == 1


def test_gpu_delegation_requires_running_turn_and_hides_write_only_assignment(gpu_system):
    value = gpu_system
    turn = _running_turn(value)
    task = _delegate(value, turn, "submit", gpu_request())
    value.compute.tick()
    writer = replace(value.user, scopes=frozenset({"agent.tasks:cancel"}))
    _, response = value.service.dispatch("POST", PREFIX + "/tasks/" + task["task_id"] + "/cancel",
                                         {}, {}, writer)
    assert "gpu_assignment" not in response["data"]
    value.hub.cancel_turn("team", turn, "alice")
    with pytest.raises(HubAccessError, match="active running turn"):
        _delegate(value, turn, "submit", gpu_request())


def test_inventory_batches_bound_rpc_and_delegated_pages_cover_all_devices(gpu_system):
    value = gpu_system
    value.connection.bindings = {
        f"device-{index:03}": {"instance_id": "remote", "workspaces": {"project": "/workspace", "other": "/other"}}
        for index in range(35)}
    original = value.fabric.inventory
    calls = []

    def inventory(ids):
        calls.append(list(ids))
        return original(ids)

    value.fabric.inventory = inventory
    assert len(value.compute.inventory("team")) == 35
    assert list(map(len, calls)) == [16, 16, 3]
    turn = _running_turn(value)
    calls.clear()
    seen, after = [], ""
    for _ in range(10):
        page = _delegate(value, turn, "devices", {"after": after})
        assert len(page) <= 4 and len(json.dumps(page, ensure_ascii=False).encode()) <= 60 * 1024
        if not page:
            break
        assert all(row["project_ids"] == ["project"] for row in page)
        seen.extend(row["device_id"] for row in page)
        after = page[-1]["device_id"]
    assert seen == sorted(value.connection.bindings)
    assert sum(map(len, calls)) == 35


def test_single_oversized_tool_device_fails_with_bounded_error(gpu_system):
    value = gpu_system
    original = value.fabric.inventory

    def inventory(ids):
        result = original(ids)
        for row in result["devices"]:
            row["runtimes"] = ["x" * (61 * 1024)]
        return result

    value.fabric.inventory = inventory
    turn = _running_turn(value)
    with pytest.raises(HubAccessError) as error:
        _delegate(value, turn, "devices", {})
    assert error.value.code == "device_inventory_too_large"
