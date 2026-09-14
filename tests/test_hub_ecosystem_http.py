"""Transport policy, finite diagnostics and credential boundaries."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pbatch.hub_ecosystem_http import IntegrationError, JSONTransport, private_token


@pytest.fixture
def remote(tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/other")
                self.end_headers()
                return
            bodies = {"/duplicate": b'{"ok":true,"ok":false}', "/nan": b'{"n":NaN}',
                      "/big": json.dumps({"x": "x" * 2000}).encode(), "/": b'{"ok":true}'}
            raw = bodies[self.path]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    token = tmp_path / "token"
    token.write_text("first")
    token.chmod(0o600)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = JSONTransport(f"http://127.0.0.1:{server.server_port}", str(token), max_response_bytes=1024)
        yield client, token, seen
        server.shutdown()
        thread.join(2)


def test_rotate_private_token_for_every_request(remote):
    client, token, seen = remote
    assert client.request("GET", "/") == {"ok": True}
    token.write_text("second")
    assert client.request("GET", "/") == {"ok": True}
    assert seen == ["Bearer first", "Bearer second"]


@pytest.mark.parametrize("path,code", [
    ("/redirect", "http_error"), ("/duplicate", "invalid_response"),
    ("/nan", "invalid_response"), ("/big", "response_too_large")])
def test_invalid_response_and_redirect_fail_closed(remote, path, code):
    client, _, seen = remote
    with pytest.raises(IntegrationError) as caught:
        client.request("GET", path)
    assert caught.value.code == code and not caught.value.retryable
    assert len(seen) == 1


def test_private_token_rejects_permissions_symlink_and_fifo(tmp_path):
    token = tmp_path / "token"
    token.write_text("secret")
    token.chmod(0o644)
    with pytest.raises(IntegrationError):
        private_token(str(token))
    token.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(IntegrationError):
        private_token(str(link))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(IntegrationError):
        private_token(str(fifo))


@pytest.mark.parametrize("endpoint", ["http://example.test", "http://localhost", "https://x/a", "https://u:p@x"])
def test_reject_nonlocal_plaintext_and_credential_origins(endpoint):
    with pytest.raises(ValueError):
        JSONTransport(endpoint, "/private/token")


def test_request_cannot_override_auth_or_escape_origin(remote):
    client, _, seen = remote
    with pytest.raises(ValueError):
        client.request("GET", "//attacker/path")
    with pytest.raises(ValueError):
        client.request("GET", "/", headers={"Authorization": "Bearer overridden"})
    assert not seen
