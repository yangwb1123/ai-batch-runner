"""Real HTTP/process GPU contract, with nvidia-smi as the only hardware substitute."""

import json
import os
import sys

import pytest

from test_hub_compute_fabric import compute, fabric, wait_until, _terminal

GPU_A = "GPU-aaaaaaaa-1111-2222-3333-444444444444"
GPU_B = "GPU-bbbbbbbb-1111-2222-3333-444444444444"
MIB = 1024 ** 2


@pytest.fixture
def fake_nvidia(tmp_path, monkeypatch):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    probe = binary_dir / "nvidia-smi"
    probe.write_text(
        f"#!{sys.executable}\n"
        f"print('{GPU_A}, Test GPU A, 8192, 4096, Disabled')\n"
        f"print('{GPU_B}, Test GPU B, 8192, 4096, Disabled')\n")
    probe.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)


@pytest.fixture
def gpu_compute(fake_nvidia, request):
    return request.getfixturevalue("compute")


def test_gpu_round_trip_binds_process_and_accepted_evidence(gpu_compute):
    client, session, runtime, server, worker, tmp_path = gpu_compute
    device = client.devices("project")[0]
    assert device["gpu_status"] == "available", device
    assert {card["uuid"] for card in device["gpus"]} == {GPU_A, GPU_B}
    assert all(card["schedulable"] and not card["reserved"] for card in device["gpus"])
    command = [sys.executable, "-c", "from pathlib import Path; import os; "
               "p=Path('gpu-once'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
               "print(os.environ.get('CUDA_VISIBLE_DEVICES'))"]
    body = {"argv": command, "timeout": 20,
            "resources": {"gpu_count": 2, "gpu_memory_bytes": 1024 * MIB}}
    task = client.submit(session, body, "gpu-round-trip")
    assert client.submit(session, body, "gpu-round-trip")["task_id"] == task["task_id"]
    result = wait_until(lambda: _terminal(client, task["task_id"]), timeout=25)
    assert result["state"] == "completed", result
    assignment = result["gpu_assignment"]
    assert assignment["uuids"] == [GPU_A, GPU_B]
    assert assignment["gpu_memory_bytes"] == 1024 * MIB
    assert result["result"]["stdout"].strip() == ",".join(assignment["uuids"])
    remote = next(iter(server.control.tasks.values()))
    assert remote["evidence"]["gpu_assignment"] == assignment
    assert remote["evidence_digest"] == result["result"]["evidence_digest"]
    assert (tmp_path / "gpu-once").read_text() == "x"
    assert len(server.control.tasks) == 1
    assert all(not card["reserved"] for card in client.devices("project")[0]["gpus"])
    cpu = client.submit(session, {
        "argv": [sys.executable, "-c", "import os; print(repr(os.environ.get('CUDA_VISIBLE_DEVICES')))"],
        "timeout": 15}, "cpu-after-gpu")
    cpu_result = wait_until(lambda: _terminal(client, cpu["task_id"]))
    assert cpu_result["state"] == "completed", cpu_result
    assert cpu_result["result"]["stdout"].strip() == "''"
    assert not cpu_result.get("gpu_assignment")


def test_gpu_cancel_releases_assignment_and_never_runs_late_effect(gpu_compute):
    client, session, runtime, server, worker, tmp_path = gpu_compute
    body = {"argv": [sys.executable, "-c", "from pathlib import Path; import time,os,json; "
                      "Path('gpu-started').write_text(json.dumps(os.environ['CUDA_VISIBLE_DEVICES'])); "
                      "time.sleep(30); Path('gpu-late').write_text('bad')"],
            "timeout": 40, "resources": {"gpu_count": 1, "gpu_memory_bytes": 1024 * MIB}}
    task = client.submit(session, body, "gpu-cancel")
    wait_until(lambda: (tmp_path / "gpu-started").exists(), timeout=25)
    visible = json.loads((tmp_path / "gpu-started").read_text())
    cards = client.devices("project")[0]["gpus"]
    assert [card["uuid"] for card in cards if card["reserved"]] == [visible]
    assert client.cancel(task["task_id"])["state"] == "cancel_requested"
    result = wait_until(lambda: _terminal(client, task["task_id"]), timeout=25)
    assert result["state"] == "cancelled", result
    assert result["result"]["cancelled"] is True
    assert result["gpu_assignment"]["uuids"] == [visible]
    assert not (tmp_path / "gpu-late").exists()
    device = client.devices("project")[0]
    assert device["reserved_cpu_cores"] == 0
    assert all(not card["reserved"] for card in device["gpus"])
    followup = client.submit(session, {
        "argv": [sys.executable, "-c", "print('released')"], "timeout": 15,
        "resources": {"gpu_count": 2}}, "gpu-after-cancel")
    done = wait_until(lambda: _terminal(client, followup["task_id"]), timeout=25)
    assert done["state"] == "completed", done
    assert len(done["gpu_assignment"]["uuids"]) == 2


def test_gpu_admission_ack_loss_and_control_restart_keep_assignment(fake_nvidia, fabric_http):
    from pbatch.fabric_server import ControlPlane
    server = fabric_http["httpd"]
    client, _ = fabric_http["register"]("gpu-recovery")
    resources = {"cpu_cores": 1, "memory_bytes": 0, "gpu_count": 2, "gpu_memory_bytes": MIB}
    original = client._post

    def lose_ack(path, payload, timeout=5.0):
        status, body = original(path, payload, timeout)
        return (0, {"error": "simulated lost GPU acknowledgement"}) if path == "/task/submit" else (status, body)

    client._post = lose_ack
    submitted = client.submit_task([sys.executable, "-c", "print('unused')"],
                                   request_id="gpu-recovery", resources=resources)
    assert submitted["task"] is None and submitted["transport_error"]
    committed = next(iter(server.control.tasks.values()))
    assignment, task_id = committed["gpu_assignment"], committed["task_id"]
    client._post = original
    server.control = ControlPlane(str(fabric_http["state_dir"]), auto_approve=True)
    replay = client.submit_task([sys.executable, "-c", "print('unused')"],
                                request_id="gpu-recovery", resources=resources)
    assert replay["duplicate"] and replay["task"]["task_id"] == task_id
    assert replay["task"]["gpu_assignment"] == assignment
    assert replay["task"]["status"] == "lost"
    assert len(server.control.tasks) == 1


def test_gpu_requires_runner_capability_while_cpu_remains_compatible(fake_nvidia, fabric_http):
    client, _ = fabric_http["register"]("old-runner")
    device = fabric_http["httpd"].control.devices[client.device_id]
    device["runner_capabilities"].remove("task.gpu.v1")
    gpu = client.submit_task([sys.executable, "-c", "print('unused')"],
                             request_id="old-gpu", resources={
                                 "cpu_cores": 1, "memory_bytes": 0, "gpu_count": 1})
    assert gpu["task"] is None and "task.gpu.v1" in gpu["error"]
    cpu = client.submit_task([sys.executable, "-c", "print('cpu')"],
                             request_id="old-cpu", resources={"cpu_cores": 1, "memory_bytes": 0})
    assert cpu["task"] is not None and not cpu["task"].get("gpu_assignment")
