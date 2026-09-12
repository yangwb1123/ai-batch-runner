"""Mobile operations control is bounded, exact-session, and token protected."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from pbatch.mobile_control import MobileSessionManager
from pbatch.ops_serve import OpsHandler
from pbatch.tui_protocol import RuntimeEvent


class FakeSession:
    def __init__(self, options):
        self.options = options
        self.listener = None
        self.closed = False
        self.aborted = False

    def subscribe(self, listener):
        self.listener = listener
        return lambda: None

    def prompt(self, prompt, timeout=None):
        del timeout
        self.listener(RuntimeEvent(
            "evt_started", 1, "now", self.options.workspace_id,
            self.options.session_id, "run_1", "turn_1", "agent_main",
            "turn.started", {}))
        self.listener(RuntimeEvent(
            "evt_delta", 2, "now", self.options.workspace_id,
            self.options.session_id, "run_1", "turn_1", "agent_main",
            "message.delta", {"text": "done"}))
        self.listener(RuntimeEvent(
            "evt_done", 3, "now", self.options.workspace_id,
            self.options.session_id, "run_1", "turn_1", "agent_main",
            "turn.completed", {"reason": "ok"}))
        return SimpleNamespace(succeeded=True, text="done", reason="ok")

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True


def _factory(options):
    return FakeSession(options)


def test_control_manager_uses_exact_server_session_and_bounds_prompt(tmp_path):
    manager = MobileSessionManager(str(tmp_path), session_factory=_factory)
    created = manager.create("phone")
    session_id = created["session_id"]
    assert created["managed"] is True
    assert created["controllable"] is True

    job = manager.start_turn(session_id, "run this")
    deadline = time.monotonic() + 2
    while manager.job(job["job_id"])["state"] == "running":
        if time.monotonic() > deadline:
            raise AssertionError("fake control turn did not finish")
        time.sleep(0.01)
    result = manager.job(job["job_id"])
    assert result["state"] == "completed"
    assert manager.detail(session_id)["turns"][0]["text"] == "done"
    with pytest.raises(KeyError):
        manager.start_turn("mb-not-a-managed-session", "no")
    with pytest.raises(ValueError):
        manager.start_turn(session_id, "x" * (16 * 1024 + 1))
    manager.close()


def _request(base, method="GET", path="", payload=None, token="secret"):
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=body, method=method)
    request.add_header("Authorization", "Bearer " + token)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode())


def test_control_http_lists_creates_and_runs_exact_session(tmp_path):
    manager = MobileSessionManager(str(tmp_path), session_factory=_factory)
    OpsHandler.cwd = str(tmp_path)
    OpsHandler.data_dir = str(tmp_path / "runtime")
    OpsHandler.token = "secret"
    OpsHandler.control = manager
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, created = _request(base, "POST", "/api/sessions", {"name": "mobile"})
        assert status == 201
        session_id = created["session_id"]
        status, job = _request(
            base, "POST", f"/api/sessions/{session_id}/turn", {"prompt": "hello"})
        assert status == 202
        deadline = time.monotonic() + 2
        while True:
            _, current = _request(base, "GET", f"/api/jobs/{job['job_id']}")
            if current["state"] == "completed":
                break
            if time.monotonic() > deadline:
                raise AssertionError("HTTP control turn did not finish")
            time.sleep(0.01)
        _, detail = _request(base, "GET", f"/api/sessions/{session_id}")
        assert detail["turns"][0]["text"] == "done"
    finally:
        server.shutdown()
        server.server_close()
        manager.close()
        OpsHandler.control = None


def test_ops_cli_forwards_mobile_control_flags(monkeypatch):
    import pbatch.ops as ops

    captured = {}
    monkeypatch.setattr(
        "pbatch.ops_serve.serve_main",
        lambda argv: (captured.setdefault("argv", argv), 0)[1])
    assert ops.ops_main([
        "serve", "--control", "--token", "secret", "--token-file", "token.txt",
        "--runtime", "legacy", "--tls-cert", "cert.pem", "--tls-key", "key.pem",
    ]) == 0
    assert "--control" in captured["argv"]
    assert "--runtime=legacy" in captured["argv"]
    assert "--token-file=token.txt" in captured["argv"]


def test_control_http_rejects_missing_token(tmp_path):
    OpsHandler.cwd = str(tmp_path)
    OpsHandler.data_dir = str(tmp_path / "runtime")
    OpsHandler.token = "secret"
    OpsHandler.control = MobileSessionManager(str(tmp_path), session_factory=_factory)
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}/api/sessions")
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        OpsHandler.control.close()
        OpsHandler.control = None
