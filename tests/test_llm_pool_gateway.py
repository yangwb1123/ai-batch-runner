"""Real HTTP gateway regressions; all credentials and upstreams are test-only."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from pbatch.llm_pool_config import PoolConfig
from pbatch.llm_pool_store import PoolAccount


pytestmark = pytest.mark.scenario
TOKEN = "test-only-local-token-0123456789"
KEYS = tuple(f"test-only-provider-{name}-0123456789" for name in "ABCD")
MODELS = {"chat-model": "chat/completions", "message-model": "messages",
          "response-model": "responses"}
RAW = b'{ "model": "chat-model", "messages": [{"role":"user","content":"hi"}] }\n'
OK = b'{ "id": "fake-result", "choices": [] }\n'
QUOTA = b'{"error":{"code":"fixture_quota"}}'


@dataclass
class Reply:
    status: int = 200
    body: bytes = OK
    headers: dict = field(default_factory=dict)
    chunks: tuple = ()  # (delay in seconds, raw bytes), after the initial body
    hold: object = None  # Optional event to block before sending headers.
    drop: bool = False
    entered: threading.Event = field(default_factory=threading.Event)
    sent: threading.Event = field(default_factory=threading.Event)


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.calls.append(SimpleNamespace(
            path=self.path, body=raw,
            headers={key.lower(): value for key, value in self.headers.items()}))
        try:
            reply = self.server.replies.get_nowait()
        except queue.Empty:
            self.server.errors.append("unexpected upstream request")
            reply = Reply(500, b'{"error":"unexpected request"}')
        reply.entered.set()
        while reply.hold is not None and not reply.hold.wait(0.01):
            if self.server.stopping.is_set():
                return
        self.close_connection = True
        if reply.drop:
            return  # Close after receiving the request: execution is ambiguous.
        try:
            self._respond(reply)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when the gateway deadline closes a stream.

    def _respond(self, reply):
        self.send_response(reply.status)
        headers = {"Content-Type": "application/json", "Connection": "close",
                   **reply.headers}
        if not reply.chunks:
            headers.setdefault("Content-Length", str(len(reply.body)))
        for name, value in headers.items():
            if value is not None:
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(reply.body)
        self.wfile.flush()
        reply.sent.set()
        for delay, chunk in reply.chunks:
            if self.server.stopping.wait(delay):
                return
            self.wfile.write(chunk)
            self.wfile.flush()


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive(), "test HTTP server failed to stop"


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    server.replies, server.calls, server.errors = queue.Queue(), [], []
    server.stopping = threading.Event()
    server.url = f"http://127.0.0.1:{server.server_port}/v1"
    with serving(server):
        try:
            yield server
        finally:
            server.stopping.set()
    assert not server.errors


def private_file(directory, name, value):
    path = directory / name
    path.write_text(value, encoding="ascii")
    path.chmod(0o600)
    return path


class GatewayClient:
    def __init__(self, server):
        self.server = server

    def open(self, body=RAW, *, path="/v1/chat/completions", headers=None,
             extra=(), method="POST"):
        outgoing = {"Authorization": "Bearer " + TOKEN,
                    "Content-Type": "application/json", "Content-Length": str(len(body)),
                    "x-opencode-session": "session-test-123", **(headers or {})}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port,
                                                timeout=5)
        try:
            connection.putrequest(method, path, skip_accept_encoding=True)
            for name, value in list(outgoing.items()) + list(extra):
                if value is not None:
                    connection.putheader(name, value)
            connection.endheaders(body)
            return connection, connection.getresponse()
        except BaseException:
            connection.close()
            raise

    def request(self, *args, **kwargs):
        connection, response = self.open(*args, **kwargs)
        try:
            return SimpleNamespace(status=response.status, body=response.read(),
                                   headers=dict((k.lower(), v) for k, v in response.getheaders()))
        finally:
            response.close()
            connection.close()

    def idle(self, timeout=1):
        end = time.monotonic() + timeout
        while True:
            status = self.server.store.status(self.server.settings.accounts)
            if status["in_flight"] == 0:
                return status
            assert time.monotonic() < end, f"gateway leaked account leases: {status}"
            threading.Event().wait(0.01)


@pytest.fixture
def gateway_factory(tmp_path, upstream):
    from pbatch.llm_pool_gateway import PoolGateway

    with ExitStack() as stack:
        created = []

        def make(*, groups=("group-a", "group-b"), keys=KEYS, **settings):
            directory = tmp_path / f"pool-{len(created)}"
            directory.mkdir(mode=0o700)
            token_file = private_file(directory, "local-token", TOKEN)
            accounts = tuple(PoolAccount(
                id=f"account-{i}", credential_file=str(private_file(directory, f"key-{i}", keys[i])),
                quota_group=group, priority=i) for i, group in enumerate(groups))
            config = PoolConfig("test-pool", accounts, MODELS, token_file,
                                directory / "state.sqlite3", **settings)
            server = stack.enter_context(serving(PoolGateway(config, port=0,
                                                            upstream_url=upstream.url)))
            client = GatewayClient(server)
            created.append(client)
            return client

        yield make
        for client in created:
            client.idle()


@pytest.fixture
def quota_classifier(monkeypatch):
    """Only orchestration tests use this synthetic signature; real Go is tested below."""
    from pbatch import llm_pool_gateway

    def classify(status, headers, body):
        assert status == 429 and body == QUOTA
        retry = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
        return SimpleNamespace(kind="quota", code="fixture_quota", scope="account",
                               window="five_hour", retry_at=time.time() + int(retry) if retry else None)

    monkeypatch.setattr(llm_pool_gateway, "classify_failure", classify)


@pytest.mark.parametrize("model,route", MODELS.items())
@pytest.mark.parametrize("auth_header", ["Authorization", "x-api-key"])
def test_protocol_preserves_bytes_and_session_but_replaces_credentials(
        gateway_factory, upstream, model, route, auth_header):
    gateway = gateway_factory()
    raw = ('{ "model": "%s", "messages": [], "input": "café", "tools": [] }\n'
           % model).encode("utf-8")
    upstream.replies.put(Reply(headers={"Set-Cookie": "provider-cookie=secret"}))
    headers = {"Cookie": "local-cookie=private", "Proxy-Authorization": "Bearer " + TOKEN,
               "x-forwarded-authorization": TOKEN, "x-opencode-session": "stable-session-42"}
    if auth_header == "x-api-key":
        headers.update({"Authorization": None, "x-api-key": TOKEN})
    result = gateway.request(raw, path="/v1/" + route, headers=headers)
    assert result.status == 200 and result.body == OK
    assert "set-cookie" not in result.headers
    assert len(upstream.calls) == 1
    call = upstream.calls[0]
    assert call.path == "/v1/" + route and call.body == raw
    assert call.headers["x-opencode-session"] == "stable-session-42"
    assert call.headers["authorization"] == "Bearer " + KEYS[0]
    assert call.headers.get("x-api-key") == (KEYS[0] if route == "messages" else None)
    assert TOKEN not in json.dumps(call.headers)
    assert {"cookie", "proxy-authorization", "x-forwarded-authorization"}.isdisjoint(call.headers)


def test_quota_switches_accounts_within_one_request(gateway_factory, upstream, quota_classifier):
    gateway = gateway_factory()
    upstream.replies.put(Reply(429, QUOTA, {"Retry-After": "120"}))
    upstream.replies.put(Reply())
    result = gateway.request()
    assert result.status == 200 and result.body == OK
    assert [call.headers["authorization"] for call in upstream.calls] == [
        "Bearer " + KEYS[0], "Bearer " + KEYS[1]]
    assert all(call.body == RAW for call in upstream.calls)
    assert {call.headers["x-opencode-session"] for call in upstream.calls} == {"session-test-123"}
    assert gateway.idle()["in_flight"] == 0


@pytest.mark.parametrize("window", ["5 hour", "weekly", "monthly"])
def test_real_go_classifier_switches_accounts_end_to_end(gateway_factory, upstream, window):
    gateway = gateway_factory()
    # Official Go handler.ts envelope: metadata is top-level, no Content-Type.
    error = {"type": "error", "error": {"type": "GoUsageLimitError",
             "message": "OpenCode Go usage limit exceeded."},
             "metadata": {"workspace": "wrk_fixture", "limitName": window}}
    upstream.replies.put(Reply(429, json.dumps(error).encode(),
                               {"Retry-After": "120", "Content-Type": None}))
    upstream.replies.put(Reply())
    result = gateway.request()
    assert result.status == 200 and result.body == OK
    assert [call.headers["authorization"] for call in upstream.calls] == [
        "Bearer " + KEYS[0], "Bearer " + KEYS[1]]
    assert all(call.body == RAW for call in upstream.calls)
    state = gateway.idle()
    blocked = next(account for account in state["accounts"] if account["id"] == "account-0")
    assert blocked["state"] == "blocked"
    assert blocked["blocks"][0]["reset_known"] is True
    assert blocked["blocks"][0]["window"] == {"5 hour": "5h"}.get(window, window)
    assert any(event["kind"] == "quota" for event in gateway.server.store.events())


@pytest.mark.parametrize("duplicate_key", [False, True])
def test_quota_skips_all_keys_in_same_group(gateway_factory, upstream, quota_classifier, duplicate_key):
    keys = (KEYS[0], KEYS[0] if duplicate_key else KEYS[1], KEYS[2])
    gateway = gateway_factory(groups=("shared", "shared", "independent"), keys=keys)
    upstream.replies.put(Reply(429, QUOTA))
    upstream.replies.put(Reply())
    assert gateway.request().status == 200
    assert [call.headers["authorization"] for call in upstream.calls] == [
        "Bearer " + KEYS[0], "Bearer " + KEYS[2]]
    gateway.idle()
    upstream.replies.put(Reply())
    assert gateway.request().status == 200
    assert upstream.calls[-1].headers["authorization"] == "Bearer " + KEYS[2]


def test_all_exhausted_returns_429_and_earliest_known_retry(gateway_factory, upstream, quota_classifier):
    gateway = gateway_factory()
    upstream.replies.put(Reply(429, QUOTA, {"Retry-After": "120"}))
    upstream.replies.put(Reply(429, QUOTA, {"Retry-After": "300"}))
    result = gateway.request()
    assert result.status == 429
    assert json.loads(result.body)["error"]["code"] == "pool_unavailable"
    assert 115 <= int(result.headers["retry-after"]) <= 121
    assert gateway.request().status == 429
    assert len(upstream.calls) == 2


@pytest.mark.parametrize("status,code,expected", [
    (429, "rate_limit_exceeded", 429), (401, "invalid_api_key", 401),
    (403, "access_denied", 403), (500, "server_error", 502), (503, "overloaded", 502),
])
def test_nonquota_failures_never_replay_and_redact_secrets(
        gateway_factory, upstream, capsys, status, code, expected):
    gateway = gateway_factory()
    raw = json.dumps({"error": {"code": code, "message": KEYS[0] + " " + TOKEN}}).encode()
    upstream.replies.put(Reply(status, raw, {"X-Debug-Key": KEYS[0], "Set-Cookie": KEYS[0]}))
    result = gateway.request()
    assert result.status == expected
    assert len(upstream.calls) == 1
    state = gateway.idle()
    logs = capsys.readouterr()
    public = result.body.decode() + json.dumps(result.headers) + json.dumps(state) + logs.out + logs.err
    public += json.dumps(gateway.server.store.events())
    assert all(secret not in public for secret in (TOKEN, *KEYS))


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_provider_redirect_is_not_followed(gateway_factory, upstream, status):
    gateway = gateway_factory()
    upstream.replies.put(Reply(status, b"", {"Location": upstream.url + "/redirect-target"}))
    result = gateway.request()
    assert result.status == 502
    assert "location" not in result.headers
    assert len(upstream.calls) == 1


def test_transport_disconnect_after_receiving_body_is_not_replayed(gateway_factory, upstream):
    gateway = gateway_factory()
    upstream.replies.put(Reply(drop=True))
    result = gateway.request()
    assert result.status == 502
    assert json.loads(result.body)["error"]["code"] == "pool_transport_failed"
    assert len(upstream.calls) == 1 and upstream.calls[0].body == RAW
    gateway.idle()
    events = gateway.server.store.events()
    assert any(event["kind"] == "service" for event in events)
    assert not any(event["kind"] == "success" for event in events)
    upstream.replies.put(Reply())
    assert gateway.request().status == 200


def test_sse_is_forwarded_before_upstream_finishes(gateway_factory, upstream):
    gateway = gateway_factory()
    first = b': heartbeat\r\nevent: message\r\ndata: {"delta":"caf\xc3\xa9"}\r\n\r\n'
    tail = b'data: [DONE]\n\n'
    reply = Reply(body=first, headers={"Content-Type": "text/event-stream"}, chunks=((0.5, tail),))
    upstream.replies.put(reply)
    connection, response = gateway.open(b'{"model":"chat-model","stream":true}')
    try:
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream"
        assert response.read(len(first)) == first
        assert gateway.server.store.status(gateway.server.settings.accounts)["in_flight"] == 1
        assert response.read() == tail
    finally:
        response.close()
        connection.close()
    assert len(upstream.calls) == 1
    gateway.idle()


def test_sse_midstream_quota_error_does_not_switch_accounts(gateway_factory, upstream):
    gateway = gateway_factory()
    first = b'data: {"delta":"partial answer"}\n\n'
    error = b'event: error\ndata: {"error":{"code":"insufficient_quota"}}\n\n'
    upstream.replies.put(Reply(body=first, headers={"Content-Type": "text/event-stream"},
                               chunks=((0.01, error),)))
    result = gateway.request(b'{"model":"chat-model","stream":true}')
    assert result.status == 200 and result.body == first + error
    assert len(upstream.calls) == 1
    gateway.idle()
