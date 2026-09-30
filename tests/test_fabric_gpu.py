"""Bounded NVIDIA probe, GPU admission DTO and child visibility tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from pbatch import fabric, fabric_local, fabric_runner
from pbatch.fabric import ExecutionEvidence
from pbatch.fabric_server_util import evidence_digest
from pbatch.fabric_gpu import (GPU_PROBE_OUTPUT_BYTES, cuda_visible_devices,
                               normalize_gpu_assignment,
                               probe_nvidia_gpus, select_gpu_assignment,
                               verify_gpu_assignment)
from pbatch.fabric_hub_contract import normalize_resources

GPU_A = "GPU-8932f937-d72c-4106-c12f-20bd9faed9f6"
GPU_B = "GPU-5832f937-d72c-4106-c12f-20bd9faed9f7"


def _fake_gpu_probe(uuid=GPU_A, available=16 * 1024 ** 3):
    now = time.time()
    return {"status": "available", "observed_at": now,
            "devices": [{"uuid": uuid, "name": "NVIDIA Test",
                         "vendor": "nvidia",
                         "total_memory_bytes": 24 * 1024 ** 3,
                         "available_memory_bytes": available,
                         "mig_mode": "disabled", "status": "available",
                         "observed_at": now}]}


def _install_fake_gpu(monkeypatch, runner_probe=None):
    provider = lambda: _fake_gpu_probe()
    # The local observation lives in fabric_local, so that is the module whose
    # `probe_nvidia_gpus` binding the provider has to replace.
    monkeypatch.setattr(fabric_local, "probe_nvidia_gpus", provider)
    monkeypatch.setattr(fabric_runner, "probe_nvidia_gpus",
                        runner_probe or provider)


def _fake_nvidia_smi(tmp_path, output: str) -> tuple[dict, list]:
    executable = tmp_path / "nvidia-smi"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write({output!r})\n",
        encoding="utf-8")
    executable.chmod(0o700)
    return {"PATH": str(tmp_path)}, [str(executable)]


def test_probe_reads_only_bounded_nvidia_rows_and_filters_full_uuid(tmp_path):
    output = (f"{GPU_A}, NVIDIA A, 40960, 32000, Disabled\n"
              f"{GPU_B}, NVIDIA B, 81920, 64000, Enabled\n")
    env, _ = _fake_nvidia_smi(tmp_path, output)

    all_gpus = probe_nvidia_gpus(env=env)
    assert all_gpus["status"] == "available"
    assert [row["uuid"] for row in all_gpus["devices"]] == [GPU_A, GPU_B]
    assert all_gpus["devices"][0]["total_memory_bytes"] == 40960 * 1024 ** 2
    assert all_gpus["devices"][1]["status"] == "mig_unsupported"

    restricted = probe_nvidia_gpus(env={**env, "CUDA_VISIBLE_DEVICES": GPU_A})
    assert [row["uuid"] for row in restricted["devices"]] == [GPU_A]
    numeric = probe_nvidia_gpus(env={**env, "CUDA_VISIBLE_DEVICES": "0"})
    assert numeric["status"] == "restricted" and numeric["devices"] == []
    hidden = probe_nvidia_gpus(env={**env, "CUDA_VISIBLE_DEVICES": ""})
    assert hidden["devices"] == []


def test_probe_output_limit_fails_gpu_closed(tmp_path):
    env, _ = _fake_nvidia_smi(tmp_path, "x" * (GPU_PROBE_OUTPUT_BYTES + 1))
    children = []

    def track_popen(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        children.append(process)
        return process

    report = probe_nvidia_gpus(env=env, popen_factory=track_popen)

    assert report["status"] == "unavailable"
    assert report["devices"] == []
    assert children and children[0].poll() is not None


def test_probe_timeout_kills_process_and_does_not_affect_cpu(monkeypatch,
                                                             tmp_path):
    executable = tmp_path / "nvidia-smi"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n",
                          encoding="utf-8")
    executable.chmod(0o700)
    children = []

    def track_popen(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        children.append(process)
        return process

    report = probe_nvidia_gpus(env={"PATH": str(tmp_path)}, timeout=0.1,
                               popen_factory=track_popen)
    monkeypatch.setattr(fabric_local, "probe_nvidia_gpus",
                        lambda: {"status": "unavailable", "devices": [],
                                 "observed_at": 1.0})
    local = fabric.local_probe()

    assert report["status"] == "unavailable"
    assert children and children[0].poll() is not None
    assert local["static_capabilities"]["cpu_cores"] >= 1
    assert local["dynamic_state"]["gpu_status"] == "unavailable"


def test_cpu_resource_shape_is_stable_and_gpu_fields_are_bounded():
    cpu = {"cpu_cores": 2, "memory_bytes": 0}
    assert normalize_resources(cpu) == cpu
    assert normalize_resources({**cpu, "gpu_count": 0,
                                "gpu_memory_bytes": 0}) == cpu
    assert normalize_resources({**cpu, "gpu_count": 1}) == {
        **cpu, "gpu_count": 1, "gpu_memory_bytes": 0}
    for invalid in (
            {**cpu, "gpu_count": True},
            {**cpu, "gpu_count": 17},
            {**cpu, "gpu_memory_bytes": 1},
            {**cpu, "gpu_count": 1, "gpu_memory_bytes": 1024 ** 4 + 1}):
        with pytest.raises(ValueError):
            normalize_resources(invalid)


def test_selection_and_fresh_assignment_validation_fail_closed():
    now = 100.0
    rows = [
        {"uuid": GPU_A, "vendor": "nvidia", "total_memory_bytes": 1024,
         "available_memory_bytes": 800, "schedulable": True,
         "reserved": False, "observed_at": now},
        {"uuid": GPU_B, "vendor": "nvidia", "total_memory_bytes": 1024,
         "available_memory_bytes": 900, "schedulable": False,
         "reserved": False, "observed_at": now},
    ]
    assignment = select_gpu_assignment(
        {"gpu_count": 1, "gpu_memory_bytes": 700}, rows, set(), now)
    assert assignment == {
        "vendor": "nvidia", "mode": "physical", "uuids": [GPU_A],
        "gpu_memory_bytes": 700, "assigned_at": now, "observed_at": now}
    assert not select_gpu_assignment(
        {"gpu_count": 1, "gpu_memory_bytes": 700}, rows, {GPU_A}, now)
    zero_free = dict(rows[0], available_memory_bytes=0)
    zero_assignment = select_gpu_assignment(
        {"gpu_count": 1, "gpu_memory_bytes": 0}, [zero_free], set(), now)
    assert zero_assignment["uuids"] == [GPU_A]
    assert normalize_gpu_assignment(assignment, 1, 700) == assignment
    probe = {"status": "available", "observed_at": now,
             "devices": [{"uuid": GPU_A, "vendor": "nvidia",
                          "status": "available", "total_memory_bytes": 1024,
                          "available_memory_bytes": 750,
                          "observed_at": now}]}
    assert verify_gpu_assignment(assignment, probe, 1, 700, now=now) == ""
    probe["devices"][0]["available_memory_bytes"] = 699
    assert "insufficient free memory" in verify_gpu_assignment(
        assignment, probe, 1, 700, now=now)
    assert cuda_visible_devices(assignment) == GPU_A


def test_local_child_receives_only_selected_full_gpu_uuid():
    assignment = {"vendor": "nvidia", "mode": "physical", "uuids": [GPU_A],
                  "gpu_memory_bytes": 0, "assigned_at": 2.0,
                  "observed_at": 1.0}
    spec = fabric.ExecutionSpec(
        argv=[sys.executable, "-c",
              "import os; print(os.environ['CUDA_VISIBLE_DEVICES'])"],
        timeout=5, gpu_assignment=assignment,
        env={"CUDA_VISIBLE_DEVICES": GPU_B})

    evidence = fabric.LocalExecutionTarget().execute(spec)

    assert evidence.result == "passed"
    assert evidence.stdout.strip() == GPU_A
    assert evidence.to_dict()["gpu_assignment"] == assignment


def test_local_execution_only_hides_gpu_when_resource_task_requests_it(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", GPU_B)
    argv = [sys.executable, "-c",
            "import os; print(os.environ.get('CUDA_VISIBLE_DEVICES', '<unset>'))"]
    direct = fabric.LocalExecutionTarget().execute(
        fabric.ExecutionSpec(argv=argv, timeout=5))
    resource_task = fabric.LocalExecutionTarget().execute(
        fabric.ExecutionSpec(argv=argv, timeout=5,
                             restrict_gpu_visibility=True))

    assert direct.stdout.strip() == GPU_B
    assert resource_task.stdout.strip() == ""


def test_signed_fabric_gpu_task_runs_with_assignment_and_digest(fabric_http,
                                                                monkeypatch):
    _install_fake_gpu(monkeypatch)
    client, _ = fabric_http["register"]("gpu-runner")
    resources = {"cpu_cores": 1, "memory_bytes": 0, "gpu_count": 1,
                 "gpu_memory_bytes": 1024 ** 3}
    submitted = client.submit_task(
        [sys.executable, "-c",
         "import os; print(os.environ['CUDA_VISIBLE_DEVICES'])"],
        request_id="gpu-success", resources=resources, timeout=10)
    task = submitted["task"]
    assignment = task["gpu_assignment"]
    assert assignment["uuids"] == [GPU_A]
    assert set(assignment) == {"vendor", "mode", "uuids", "gpu_memory_bytes",
                               "assigned_at", "observed_at"}
    assert task["resources"] == resources
    assert task["mobility"] == "pinned"

    claimed = client.poll_tasks(limit=1)["tasks"][0]
    result = fabric_runner.execute_task(client, claimed)
    final = client.task_status(task["task_id"])["task"]

    assert result["accepted"] is True
    assert final["status"] == "done"
    assert final["evidence"]["stdout"].strip() == GPU_A
    assert final["evidence"]["gpu_assignment"] == assignment
    assert final["evidence_digest"] == evidence_digest(final["evidence"])


def test_resource_cpu_task_hides_inherited_cuda_devices(fabric_http,
                                                         monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", GPU_B)
    client, _ = fabric_http["register"]("cpu-gpu-visibility")
    task = client.submit_task(
        [sys.executable, "-c",
         "import os; print(os.environ.get('CUDA_VISIBLE_DEVICES', '<unset>'))"],
        request_id="cpu-no-gpu", timeout=10,
        resources={"cpu_cores": 1, "memory_bytes": 0})["task"]
    claimed = client.poll_tasks(limit=1)["tasks"][0]

    result = fabric_runner.execute_task(client, claimed)
    final = client.task_status(task["task_id"])["task"]

    assert result["accepted"] is True
    assert final["status"] == "done"
    assert final["evidence"]["stdout"].strip() == ""


def test_legacy_unbounded_task_keeps_inherited_cuda_visibility(fabric_http,
                                                               monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", GPU_B)
    client, _ = fabric_http["register"]("legacy-gpu-visibility")
    task = client.submit_task(
        [sys.executable, "-c",
         "import os; print(os.environ.get('CUDA_VISIBLE_DEVICES', '<unset>'))"],
        request_id="legacy-no-resource", timeout=10)["task"]
    claimed = client.poll_tasks(limit=1)["tasks"][0]

    result = fabric_runner.execute_task(client, claimed)
    final = client.task_status(task["task_id"])["task"]

    assert result["accepted"] is True
    assert final["status"] == "done"
    assert final["evidence"]["stdout"].strip() == GPU_B


def test_gpu_preflight_mismatch_fails_without_spawning(fabric_http,
                                                       monkeypatch,
                                                       tmp_path):
    _install_fake_gpu(monkeypatch, lambda: _fake_gpu_probe(uuid=GPU_B))
    client, _ = fabric_http["register"]("gpu-mismatch")
    marker = tmp_path / "spawned"
    task = client.submit_task(
        [sys.executable, "-c",
         f"open({str(marker)!r}, 'w', encoding='utf-8').write('ran')"],
        request_id="gpu-mismatch", timeout=10,
        resources={"cpu_cores": 1, "memory_bytes": 0, "gpu_count": 1,
                   "gpu_memory_bytes": 1024})["task"]
    claimed = client.poll_tasks(limit=1)["tasks"][0]

    result = fabric_runner.execute_task(client, claimed)
    final = client.task_status(task["task_id"])["task"]

    assert result["accepted"] is True
    assert final["status"] == "failed"
    assert "UUID is unavailable" in final["evidence"]["stderr"]
    assert not marker.exists()


def test_cancel_during_gpu_preflight_is_fenced_before_spawn(fabric_http,
                                                             monkeypatch,
                                                             tmp_path):
    _install_fake_gpu(monkeypatch)
    client, _ = fabric_http["register"]("gpu-preflight-cancel")
    marker = tmp_path / "cancelled-task-ran"
    resources = {"cpu_cores": 1, "memory_bytes": 0, "gpu_count": 1,
                 "gpu_memory_bytes": 1024}
    task = client.submit_task(
        [sys.executable, "-c",
         f"open({str(marker)!r}, 'w', encoding='utf-8').write('ran')"],
        request_id="gpu-cancel", timeout=10, resources=resources)["task"]
    claimed = client.poll_tasks(limit=1)["tasks"][0]

    def cancel_during_probe():
        assert client.task_cancel(task["task_id"])["cancel_requested"] is True
        return _fake_gpu_probe()

    monkeypatch.setattr(fabric_runner, "probe_nvidia_gpus",
                        cancel_during_probe)
    result = fabric_runner.execute_task(client, claimed)
    final = client.task_status(task["task_id"])["task"]

    assert result["accepted"] is True
    assert final["status"] == "cancelled"
    assert final["evidence"]["cancelled"] is True
    assert final["evidence"]["gpu_assignment"] == task["gpu_assignment"]
    assert not marker.exists()


def test_inventory_gpu_dto_uses_fresh_dynamic_measurement_time(fabric_http):
    client, _ = fabric_http["register"]("gpu-inventory-dto")
    control = fabric_http["httpd"].control
    device = control.devices[client.device_id]
    static = {"uuid": GPU_A, "name": "NVIDIA Test", "vendor": "nvidia",
              "total_memory_bytes": 24 * 1024 ** 3, "mig_mode": "disabled"}
    dynamic = {"uuid": GPU_A, "vendor": "nvidia", "status": "available",
               "total_memory_bytes": 24 * 1024 ** 3,
               "available_memory_bytes": 16 * 1024 ** 3,
               "observed_at": time.time() - 120}
    device["probe"]["static_capabilities"]["gpus"] = [static]
    state = dict(device["dynamic_state"], gpus=[dynamic])
    client.heartbeat(state, [])

    row = client.inventory([client.device_id])["devices"][0]
    card = row["gpus"][0]

    assert row["gpu_status"] == "stale"
    assert card["observed_at"] == dynamic["observed_at"]
    assert card["schedulable"] is False
    assert set(card) == {"uuid", "name", "vendor", "total_memory_bytes",
                         "available_memory_bytes", "schedulable", "reserved",
                         "observed_at"}
    assert "gpus" not in row["dynamic_state"]
    assert "gpus" not in row["static_capabilities"]


@pytest.mark.parametrize("mig_mode,expected", [
    ("disabled", True), ("n/a", True), ("enabled", False),
    ("", False), ("unknown", False),
])
def test_inventory_only_schedules_known_non_mig_modes(fabric_http, mig_mode,
                                                       expected):
    suffix = mig_mode.replace("/", "-") or "missing"
    client, _ = fabric_http["register"](f"gpu-mig-{suffix}")
    control = fabric_http["httpd"].control
    device = control.devices[client.device_id]
    static = {"uuid": GPU_A, "name": "NVIDIA Test", "vendor": "nvidia",
              "total_memory_bytes": 24 * 1024 ** 3}
    if mig_mode:
        static["mig_mode"] = mig_mode
    dynamic = {"uuid": GPU_A, "vendor": "nvidia", "status": "available",
               "total_memory_bytes": 24 * 1024 ** 3,
               "available_memory_bytes": 16 * 1024 ** 3,
               "observed_at": time.time()}
    device["probe"]["static_capabilities"]["gpus"] = [static]
    client.heartbeat(dict(device["dynamic_state"], gpus=[dynamic]), [])

    row = client.inventory([client.device_id])["devices"][0]

    assert row["gpus"][0]["schedulable"] is expected
    assert row["gpu_status"] == ("available" if expected else "unavailable")


def test_inventory_gpu_card_count_and_duplicate_ids_fail_closed(fabric_http):
    client, _ = fabric_http["register"]("gpu-inventory-bounds")
    control = fabric_http["httpd"].control
    device = control.devices[client.device_id]
    statics, dynamics = [], []
    for index in range(17):
        uuid = f"GPU-{index:08x}-0000-0000-0000-{index:012x}"
        statics.append({"uuid": uuid, "name": "NVIDIA Test",
                        "vendor": "nvidia", "total_memory_bytes": 4096,
                        "mig_mode": "disabled"})
        dynamics.append({"uuid": uuid, "vendor": "nvidia",
                         "status": "available", "total_memory_bytes": 4096,
                         "available_memory_bytes": 4096,
                         "observed_at": time.time()})
    device["probe"]["static_capabilities"]["gpus"] = statics
    state = dict(device["dynamic_state"], gpus=dynamics)
    client.heartbeat(state, [])
    too_many = client.inventory([client.device_id])["devices"][0]
    assert too_many["gpu_status"] == "unavailable"
    assert too_many["gpus"] == []

    device["probe"]["static_capabilities"]["gpus"] = [
        statics[0], dict(statics[0], uuid=statics[0]["uuid"].upper())]
    client.heartbeat(dict(device["dynamic_state"], gpus=[
        dynamics[0], dict(dynamics[0], uuid=dynamics[0]["uuid"].upper())]), [])
    duplicate = client.inventory([client.device_id])["devices"][0]
    assert duplicate["gpu_status"] == "unavailable"
    assert duplicate["gpus"] == []


def test_gpu_assignment_tampering_is_rejected_before_evidence_commit(fabric_http,
                                                                     monkeypatch):
    _install_fake_gpu(monkeypatch)
    client, _ = fabric_http["register"]("gpu-evidence")
    submitted = client.submit_task(
        [sys.executable, "-c", "print('unused')"],
        request_id="gpu-evidence", timeout=10,
        resources={"cpu_cores": 1, "memory_bytes": 0, "gpu_count": 1,
                   "gpu_memory_bytes": 1024})["task"]
    lease = client.poll_tasks(limit=1)["tasks"][0]["lease"]
    assert client.task_start(submitted["task_id"], lease)["ok"] is True
    changed = {"vendor": "nvidia", "mode": "physical", "uuids": [GPU_B],
               "gpu_memory_bytes": 1024,
               "assigned_at": submitted["gpu_assignment"]["assigned_at"],
               "observed_at": submitted["gpu_assignment"]["observed_at"]}
    evidence = ExecutionEvidence(
        attempt_id="tampered", target_id="local", label="tampered",
        result="failed", exit_code=1, timed_out=False, output_overflow=False,
        stdout="", stderr="failure", code_digest="",
        environment_digest=fabric.local_probe()["environment_digest"],
        input_digest="", output_digest=fabric._digest("failure"),
        runner_version=fabric.FABRIC_VERSION, elapsed=0.01,
        gpu_assignment=changed).to_dict()

    result = client.evidence(submitted["task_id"], lease["lease_id"],
                             lease["fencing_token"], evidence,
                             task_id=submitted["task_id"])

    assert result["accepted"] is False
    assert "does not match" in result["reason"]
    assert client.task_status(submitted["task_id"])["task"]["status"] == "running"
