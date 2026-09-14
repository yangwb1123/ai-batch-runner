"""Suite-wide isolation for process-shared runtime state."""

from __future__ import annotations

import threading

import pytest


@pytest.fixture(autouse=True)
def _isolate_provider_breaker(monkeypatch, tmp_path_factory):
    """Prevent fake-provider failures leaking across tests or into a repo."""
    state_dir = tmp_path_factory.mktemp("provider-breaker")
    monkeypatch.setenv(
        "PBATCH_PROVIDER_BREAKER_FILE",
        str(state_dir / "state.json"),
    )


@pytest.fixture
def fabric_http(tmp_path):
    """Reusable real signed-HTTP Fabric control plane for integration tests."""
    from pbatch import fabric, fabric_runner, fabric_server
    from pbatch.fabric_rpc import RpcClient

    state_dir = tmp_path / "fabric-state"
    httpd, port = fabric_server.start_server(
        0, str(state_dir), auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{port}"

    def register(name: str):
        key = fabric_runner.load_or_create_key(str(state_dir), name)
        client = RpcClient(origin, key["secret"], key["device_id"])
        probe = fabric.local_probe("full")
        result = client.register(name, probe)
        assert result.get("approved") is True
        client.heartbeat(probe["dynamic_state"], [])
        return client, key

    yield {"httpd": httpd, "origin": origin, "state_dir": state_dir,
           "register": register}
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
