"""Placement explanations preserve the existing resource admission predicate."""

from copy import deepcopy
from itertools import product

import pytest

from pbatch.hub_compute_inventory import device_view, eligible, placement_reasons
from pbatch.hub_compute_models import request_body
from test_hub_compute import Fabric


def task():
    return dict(request_body({"argv": ["true"]}), project_id="project", _targets=["worker"])


def device():
    raw = Fabric().inventory(["worker"])["devices"][0]
    return device_view(raw, {"instance_id": "remote", "workspaces": {"project": "/work"}})


@pytest.mark.parametrize("changes,reason", [
    ({"online": False, "schedulable": False}, "device_offline"),
    ({"lifecycle_state": "draining", "schedulable": False}, "device_not_ready"),
    ({"reserved_cpu_cores": 1, "schedulable": False}, "device_busy"),
    ({"running_tasks": 1, "schedulable": False}, "device_busy"),
    ({"schedulable": False}, "device_unavailable"),
    ({"project_ids": ["other"]}, "project_unavailable"),
    ({"cpu_cores": 0}, "cpu_insufficient"),
    ({"runtimes": []}, "runtime_missing"),
    ({"os": "Windows"}, "os_mismatch"),
    ({"architecture": "arm64"}, "architecture_mismatch"),
])
def test_each_observable_device_blocker(changes, reason):
    request = task()
    request["requirements"] = {"os": "Linux", "architecture": "x86_64", "runtimes": ["python"]}
    projected = dict(device(), **changes)
    assert placement_reasons(request, projected) == [reason]
    assert not eligible(request, projected)


@pytest.mark.parametrize("changes,reason", [
    ({"_targets": []}, "target_not_authorized"),
    ({"target_device_id": "another"}, "target_mismatch"),
    ({"workspace": {"snapshot_id": "fixed"}}, "workspace_unsupported"),
])
def test_task_authority_target_and_workspace_are_not_ignored(changes, reason):
    request = dict(task(), **changes)
    assert placement_reasons(request, device()) == [reason]
    assert not eligible(request, device())


@pytest.mark.parametrize("available,total,reserved,requested,accepted", [
    (8, 16, 0, 8, True), (7, 16, 0, 8, False), (16, 16, 9, 8, False),
    (16, 16, 8, 8, True), (16, 16, 17, 0, False),
])
def test_memory_uses_lower_of_probe_and_reservation_budget(available, total, reserved, requested, accepted):
    request, projected = task(), device()
    request["resources"]["memory_bytes"] = requested
    projected.update(available_memory_bytes=available, total_memory_bytes=total,
                     reserved_memory_bytes=reserved)
    assert placement_reasons(request, projected) == ([] if accepted else ["memory_insufficient"])
    assert eligible(request, projected) is accepted


def test_resources_can_have_multiple_independent_blockers_without_duplicates():
    request, projected = task(), device()
    request["resources"].update(cpu_cores=16, memory_bytes=20_000_000)
    request["requirements"]["runtimes"] = ["node", "java"]
    projected.update(schedulable=False, reserved_cpu_cores=2, running_tasks=1)
    assert placement_reasons(request, projected) == [
        "device_busy", "cpu_insufficient", "memory_insufficient", "runtime_missing"]


@pytest.mark.parametrize("raw_change,reason", [
    ({"cordoned": True}, "device_unavailable"),
    ({"reserved_execution_slots": 1}, "device_unavailable"),
    ({"capability_valid_until": "2000-01-01T00:00:00Z"}, "device_unavailable"),
    ({"capability_valid_until": "invalid"}, "device_unavailable"),
])
def test_unexposed_fabric_facts_are_not_guessed_or_added_to_public_dto(raw_change, reason):
    raw = Fabric().inventory(["worker"])["devices"][0]
    raw.update(raw_change)
    projected = device_view(raw, {"instance_id": "remote", "workspaces": {"project": "/work"}})
    assert placement_reasons(task(), projected) == [reason]
    assert not any(key in projected for key in raw_change)


@pytest.mark.parametrize("status", ["unsupported", "unavailable", "stale", "busy"])
def test_gpu_unavailable_has_no_effect_on_cpu_requests(status):
    projected = dict(device(), gpu_status=status, gpus=[])
    assert eligible(task(), projected)
    request = task()
    request["resources"].update(gpu_count=1, gpu_memory_bytes=4)
    assert placement_reasons(request, projected) == ["gpu_unavailable"]


@pytest.mark.parametrize("count,memory,reserved,card_schedulable,accepted", [
    (1, 4, False, True, True), (1, 5, False, True, False),
    (2, 4, False, True, False), (1, 4, True, True, False), (1, 4, False, False, False),
])
def test_gpu_count_per_card_memory_and_availability(count, memory, reserved, card_schedulable, accepted):
    request, projected = task(), device()
    request["resources"].update(gpu_count=count, gpu_memory_bytes=memory)
    projected.update(gpu_status="available", gpus=[{
        "available_memory_bytes": 4, "reserved": reserved, "schedulable": card_schedulable}])
    assert placement_reasons(request, projected) == ([] if accepted else ["gpu_insufficient"])


def test_schedulable_projection_remains_the_authoritative_availability_decision():
    projected = dict(device(), online=False, lifecycle_state="unknown", running_tasks=9)
    # Inconsistent synthetic values cannot add a new condition to the old predicate.
    assert projected["schedulable"] is True
    assert eligible(task(), projected)


def test_cpu_resource_and_runtime_boundary_sweep_matches_prior_predicate():
    for scheduled, cpu, reserved, free, memory, runtime in product(
            [False, True], [0, 1, 4], [0, 1], [0, 2], [0, 2], [False, True]):
        request, projected = task(), deepcopy(device())
        request["resources"] = {"cpu_cores": 1, "memory_bytes": memory}
        request["requirements"]["runtimes"] = ["python"]
        projected.update(schedulable=scheduled, cpu_cores=cpu, reserved_cpu_cores=reserved,
                         available_memory_bytes=free, runtimes=["python"] if runtime else [])
        expected = scheduled and cpu - reserved >= 1 and free >= memory and runtime
        assert eligible(request, projected) is expected


@pytest.mark.parametrize("name", ["cpu_cores", "total_memory_bytes", "available_memory_bytes",
                                  "reserved_cpu_cores", "reserved_memory_bytes", "running_tasks"])
def test_invalid_projected_numbers_fail_closed_for_both_diagnostics_and_scheduler(name):
    for value in (float("nan"), float("inf"), -1, True, None, "8", 10**1000):
        projected = dict(device(), **{name: value})
        assert placement_reasons(task(), projected) == ["device_unavailable"]
        assert not eligible(task(), projected)


@pytest.mark.parametrize("name,value", [("os", []), ("architecture", None), ("runtimes", "python"),
                                        ("runtimes", [1]), ("runtimes", ["\ud800"]),
                                        ("project_ids", {"project"}), ("schedulable", 1),
                                        ("workspace_supported", "yes")])
def test_invalid_projected_types_fail_closed_before_resource_comparisons(name, value):
    projected = dict(device(), **{name: value})
    assert placement_reasons(task(), projected) == ["device_unavailable"]
    assert not eligible(task(), projected)


@pytest.mark.parametrize("section,name", [("resources", "cpu_cores"), ("resources", "memory_bytes"),
                                         ("resources", "reserved_cpu_cores"),
                                         ("resources", "reserved_memory_bytes"),
                                         ("dynamic_state", "available_memory_bytes"),
                                         ("dynamic_state", "running_tasks")])
def test_invalid_raw_numbers_cannot_become_safe_zero_budget_facts(section, name):
    for value in (float("nan"), float("inf"), -1, True, None, "8", 10**1000):
        raw = Fabric().inventory(["worker"])["devices"][0]
        raw[section][name] = value
        projected = device_view(raw, {"instance_id": "remote", "workspaces": {"project": "/work"}})
        assert projected["schedulable"] is False
        assert placement_reasons(task(), projected)
        assert not eligible(task(), projected)


@pytest.mark.parametrize("runtimes", [None, "python", [None], ["\ud800"]])
def test_invalid_runtime_inventory_is_unavailable_even_without_a_runtime_requirement(runtimes):
    raw = Fabric().inventory(["worker"])["devices"][0]
    raw["runtimes"] = runtimes
    projected = device_view(raw, {"instance_id": "remote", "workspaces": {"project": "/work"}})
    assert projected["runtimes"] == [] and projected["schedulable"] is False
    assert placement_reasons(task(), projected) == ["device_unavailable"]
