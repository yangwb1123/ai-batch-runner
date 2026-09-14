"""Real Hub -> signed Fabric HTTP -> Runner -> process -> accepted evidence."""

import hashlib
import json
import sys
import threading
import time

import pytest

from pbatch.fabric_rpc import RpcClient
from pbatch.fabric_runner import load_or_create_key, run_runner_loop
from pbatch.fabric_server import start_server
from pbatch.hub_auth import HubAuth, USER_SCOPES
from pbatch.hub_client import HubClient
from pbatch.hub_compute import HubCompute
from pbatch.hub_compute_client import ComputeClient
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore


def wait_until(check, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("timed out waiting for real compute execution")


@pytest.fixture
def fabric(tmp_path):
    worker_dir = tmp_path / "runner"
    worker = load_or_create_key(str(worker_dir), "worker")
    policy = {"trust_zone": "trusted-local", "trust_level": 3,
              "max_effect_class": "irreversible", "allowed_data_classes": ["internal", "public"]}
    server, port = start_server(0, str(tmp_path / "control"), auto_approve=True,
                                device_policies={worker["device_id"]: policy})
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    url = f"http://127.0.0.1:{port}"
    service = RpcClient(url, "service-secret", "hub-service")
    assert service.register("hub-service")["approved"]
    stop = threading.Event()
    runner = threading.Thread(target=run_runner_loop, args=(url, "worker", str(worker_dir)),
                              kwargs={"stop_event": stop, "heartbeat_interval": 0.05}, daemon=True)
    runner.start()
    wait_until(lambda: worker["device_id"] in server.control.devices and
                server.control.devices[worker["device_id"]].get("heartbeats", 0) > 0)
    yield server, url, worker, service
    for task_id in list(server.control.tasks):
        service.task_cancel(task_id)
    stop.set()
    runner.join(15)
    server.shutdown()
    server.server_close()
    serving.join(5)
    assert not runner.is_alive()


@pytest.fixture
def compute(tmp_path, fabric):
    server, url, worker, service = fabric
    credential = tmp_path / "service.json"
    credential.write_text(json.dumps({"device_id": service.device_id, "secret": service.secret}))
    credential.chmod(0o600)
    store = HubStore(tmp_path / "hub.sqlite")
    store.register_instance("team", "source", {"boot_id": "boot"})
    session = store.upsert_session("team", "source", 1, {
        "local_session_id": "local", "project_id": "project", "controllable": True, "status": "idle"})
    runtime = HubCompute(store, [{"tenant_id": "team", "base_url": url,
                                  "credential_file": str(credential), "bindings": [{
                                      "device_id": worker["device_id"], "instance_id": "worker-instance",
                                      "workspaces": {"project": str(tmp_path)}}]}])
    grant = {"tenant_id": "team", "subject": "alice", "client_id": "console", "kind": "user",
             "instances": ["source"], "compute_instances": ["worker-instance"], "scopes": list(USER_SCOPES),
             "token_sha256": hashlib.sha256(b"user").hexdigest()}
    httpd = HubHTTPServer(("127.0.0.1", 0), store, HubAuth({"mode": "development", "grants": [grant]}),
                          compute=runtime)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    runtime.start()
    client = ComputeClient(HubClient(f"http://127.0.0.1:{httpd.server_port}", "user"))
    yield client, session["session_id"], runtime, server, worker, tmp_path
    runtime.close()
    httpd.shutdown()
    httpd.server_close()
    thread.join(5)
    store.close()


def test_real_remote_execution_and_content_idempotency(compute):
    client, session, runtime, server, worker, tmp_path = compute
    devices = client.devices("project")
    assert devices[0]["device_id"] == worker["device_id"]
    assert "python3" in devices[0]["runtimes"]
    command = [sys.executable, "-c", "from pathlib import Path; "
               "p=Path('once.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('computed')"]
    body = {"argv": command, "timeout": 15}
    task = client.submit(session, body, "real-request")
    assert client.submit(session, body, "real-request")["task_id"] == task["task_id"]
    result = wait_until(lambda: _terminal(client, task["task_id"]))
    assert result["state"] == "completed", result
    assert result["result"]["stdout"].strip() == "computed"
    assert result["result"]["evidence_digest"]
    assert (tmp_path / "once.txt").read_text() == "x"
    assert len(server.control.tasks) == 1
    assert result["device_instance_id"] == "worker-instance"


def _terminal(client, task_id):
    task = client.get(task_id)
    return task if task["state"] in {"completed", "failed", "cancelled", "lost"} else None


def test_running_compute_cancel_kills_process_and_keeps_heartbeats(compute):
    client, session, runtime, server, worker, tmp_path = compute
    body = {"argv": [sys.executable, "-c", "from pathlib import Path; import time; "
                      "Path('started').write_text('yes'); time.sleep(30); Path('late').write_text('bad')"],
             "timeout": 40, "resources": {"cpu_cores": 1, "memory_bytes": 0}}
    task = client.submit(session, body, "cancel-request")
    wait_until(lambda: (tmp_path / "started").exists())
    before = server.control.devices[worker["device_id"]]["heartbeats"]
    wait_until(lambda: server.control.devices[worker["device_id"]]["heartbeats"] > before, timeout=10)
    cancelled = client.cancel(task["task_id"])
    assert cancelled["state"] == "cancel_requested"
    result = wait_until(lambda: _terminal(client, task["task_id"]))
    assert result["state"] == "cancelled", result
    assert result["result"]["cancelled"] is True
    assert not (tmp_path / "late").exists()
    devices = client.devices()
    assert devices[0]["reserved_cpu_cores"] == 0
