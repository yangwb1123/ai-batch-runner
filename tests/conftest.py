"""Suite-wide isolation for process-shared runtime state.

`checks/conftest.py` carries the same environment guard for its own tree; the
root file budget keeps the shared version out of the repository root.
"""

from __future__ import annotations

import os
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


# Runner defaults a test may legitimately retarget at a fake agent, restored
# afterwards. A leaked value does not fail the test that set it — it silently
# re-aims every later test in the session, which is how a temp agent path once
# reached meta routing and made an unrelated planner assertion blow up.
MUTABLE_CONFIG = (
    "AGENT_BIN", "MEMORY_MODE", "STREAM_OUTPUT", "AGENT_MODEL",
    "AGENT_PROVIDER", "AGENT_THINKING_OVERRIDE", "AGENT_LEDGER_ENABLED",
)

#: The entry shim sets this in `os.environ` while it is being imported.
MUTABLE_ENV = ("PBATCH_SCRIPT_DIR",)


@pytest.fixture(autouse=True)
def _restore_mutable_config():
    """Snapshot process-shared config globals and restore them per test.

    Restored with plain ``setattr`` on purpose: routing the restore through
    ``monkeypatch`` would record it, and monkeypatch's own finalization runs
    last and would undo it — reinstating the very leak this guards.
    """
    from pbatch import config

    saved = {name: getattr(config, name, None) for name in MUTABLE_CONFIG}
    yield
    for name, value in saved.items():
        setattr(config, name, value)


@pytest.fixture(autouse=True)
def _restore_mutable_environment():
    """Snapshot the environment the entry shim rewrites on import.

    `pi-batch.py` calls `os.environ.setdefault("PBATCH_SCRIPT_DIR", ...)` as
    soon as it is imported, so a test that loads the shim in-process leaves
    project-config discovery pointing at the tool checkout for the rest of the
    session. A gate that then reads project configuration fails for a reason
    that has nothing to do with the test that leaked.
    """
    saved = {name: os.environ.get(name) for name in MUTABLE_ENV}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


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
