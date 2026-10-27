"""Bounded SDK transport, using a local synthetic token HTTP endpoint."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pbatch.hub_errors import HubAccessError
from pbatch.hub_web_transport import SDKTransport


@pytest.fixture
def endpoint():
    state = {"body": b'{"access_token":"test","token_type":"Bearer"}',
             "status": 200, "type": "application/json", "calls": 0, "slow": False}
    stopped = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            state["calls"] += 1
            self.send_response(state["status"])
            self.send_header("Content-Type", state["type"])
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            try:
                if state["slow"]:
                    for _ in range(20):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        if stopped.wait(.4):
                            break
                else:
                    self.wfile.write(state["body"])
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    transport = SDKTransport("https://identity.test")
    transport.endpoint = f"http://127.0.0.1:{server.server_port}/token"  # test-only synthetic peer
    request = {"url": transport.endpoint, "method": "POST", "body": b"code=synthetic"}
    yield transport, request, state
    stopped.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_only_pinned_token_endpoint_and_bounded_request_are_allowed(endpoint):
    transport, request, state = endpoint
    assert transport.send(request)["status"] == 200
    for changed in ({"url": "https://evil.test/token"}, {"method": "GET"}, {"body": b"x" * 16385}):
        with pytest.raises(HubAccessError):
            transport.send({**request, **changed})
    assert state["calls"] == 1


@pytest.mark.parametrize("body,content_type", [
    (b"x" * 32769, "application/json"),
    (b'{"access_token":"one","access_token":"two"}', "application/json"),
    (b'{"bad":NaN}', "application/json"),
    (b"secret diagnostic", "text/html"),
    (b'{"access_token":', "application/json"),
])
def test_oversized_ambiguous_and_invalid_responses_fail_closed(endpoint, body, content_type):
    transport, request, state = endpoint
    state.update(body=body, type=content_type)
    with pytest.raises(HubAccessError) as error:
        transport.send(request)
    assert error.value.status == 503
    assert "secret" not in str(error.value)
    assert state["calls"] == 1


def test_redirect_is_not_followed_and_errors_not_retried(endpoint):
    transport, request, state = endpoint
    state.update(status=307, body=b'{"error":"redirect"}')
    assert transport.send(request)["status"] == 307
    assert state["calls"] == 1
    state.update(status=503, body=b'{"error":"unavailable"}')
    assert transport.send(request)["status"] == 503
    assert state["calls"] == 2


def test_continuous_body_does_not_extend_total_deadline(endpoint):
    transport, request, state = endpoint
    state["slow"] = True
    started = time.monotonic()
    with pytest.raises(HubAccessError):
        transport.send(request)
    assert time.monotonic() - started < 6.5
    assert state["calls"] == 1


def test_production_transport_never_accepts_plaintext_issuer():
    with pytest.raises(ValueError, match="HTTPS"):
        SDKTransport("http://identity.test")
