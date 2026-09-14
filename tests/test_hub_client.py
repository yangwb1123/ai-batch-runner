import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from pbatch.hub_client import HubClient, HubError, load_hub_token, validate_hub_url


class _HubHandler(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def _respond(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size) if size else b""
        self.seen.append((self.command, self.path, dict(self.headers), body))
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if self.path.endswith("/large"):
            payload = {"data": "x" * 2048}
        elif self.path.endswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
            return
        elif self.path.endswith("/gateway/register"):
            payload = {"data": {"instance": {"generation": 4}}}
        elif self.path.endswith("/turns"):
            payload = {"data": {"turn_id": "turn-1", "state": "queued"}}
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            encoded = json.dumps(payload).encode()
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        elif parsed.path.endswith("/instances"):
            if query.get("after"):
                payload = {"data": [{"instance_id": "inst-2"}], "next_cursor": ""}
            else:
                payload = {"data": [{"instance_id": "inst-1"}],
                           "next_cursor": "cursor-1"}
        elif parsed.path.endswith("/sessions"):
            payload = {"data": [{"session_id": "ses-1"}]}
        else:
            payload = {"data": {"cursor": "17"}}
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        return


@pytest.fixture
def hub_server():
    _HubHandler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_client_uses_envelopes_and_expected_routes(hub_server):
    client = HubClient(hub_server, "user-secret")
    assert client.list_instances() == [{"instance_id": "inst-1"},
                                       {"instance_id": "inst-2"}]
    assert client.list_sessions("inst-1") == [{"session_id": "ses-1"}]
    sent = client.send_prompt("ses-1", "hello", "retry-1")
    registered = client.register_instance(instance_id="inst-1", boot_id="boot",
                                          name="worker", project_id="proj")
    assert sent["turn_id"] == "turn-1"
    assert registered["instance"]["generation"] == 4
    post = [item for item in _HubHandler.seen if item[0] == "POST"]
    assert post[0][1].endswith("/sessions/ses-1/turns")
    assert post[0][2]["Idempotency-Key"] == "retry-1"
    assert post[0][2]["Authorization"] == "Bearer user-secret"
    pages = [item[1] for item in _HubHandler.seen if item[1].endswith("instances?limit=100")]
    assert pages == ["/api/v1/agent/instances?limit=100"]
    assert any("after=cursor-1" in item[1] for item in _HubHandler.seen)


def test_client_rejects_redirects_and_oversized_response(hub_server):
    client = HubClient(hub_server, "token", max_response_bytes=1024)
    with pytest.raises(HubError) as redirect:
        client._request("GET", "/redirect")
    assert redirect.value.status == 302
    with pytest.raises(HubError, match="size limit"):
        client._request("GET", "/large")


def test_http_requires_literal_loopback_and_https_elsewhere():
    assert validate_hub_url("http://127.0.0.1:8788/") == "http://127.0.0.1:8788"
    assert validate_hub_url("https://hub.example") == "https://hub.example"
    with pytest.raises(ValueError, match="literal loopback"):
        validate_hub_url("http://localhost:8788")
    with pytest.raises(ValueError, match="credentials"):
        validate_hub_url("https://user:pass@hub.example")
    with pytest.raises(ValueError, match="path"):
        validate_hub_url("https://hub.example/api")


def test_token_file_must_be_private_regular_file(tmp_path):
    path = tmp_path / "hub.token"
    path.write_text("test-bearer\n")
    os.chmod(path, 0o600)
    assert load_hub_token(environ={"PBATCH_HUB_TOKEN_FILE": str(path)}) == "test-bearer"
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        load_hub_token(environ={"PBATCH_HUB_TOKEN_FILE": str(path)})


def test_rotated_private_token_is_used_without_restarting_gateway(hub_server, tmp_path):
    path = tmp_path / "hub.token"
    path.write_text("first-token\n")
    path.chmod(0o600)
    client = HubClient.from_env(hub_server, environ={"PBATCH_HUB_TOKEN_FILE": str(path)})
    client.list_sessions()
    replacement = tmp_path / "replacement"
    replacement.write_text("rotated-token\n")
    replacement.chmod(0o600)
    replacement.replace(path)
    client.list_sessions()
    assert [row[2]["Authorization"] for row in _HubHandler.seen] == [
        "Bearer first-token", "Bearer rotated-token"]
    path.chmod(0o644)
    with pytest.raises(HubError) as denied:
        client.list_sessions()
    assert denied.value.code == "credential_unavailable"
    assert len(_HubHandler.seen) == 2
