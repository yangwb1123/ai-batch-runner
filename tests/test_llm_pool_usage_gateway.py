"""Usage preflight over real loopback HTTP; no provider or real credentials."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
import http.client
from http.server import ThreadingHTTPServer
import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from test_llm_pool_gateway import (
    FakeHandler, GatewayClient, KEYS, OK, RAW, TOKEN, Reply, gateway_factory, serving,
)


pytestmark = pytest.mark.scenario


def usage_body(full=None, *, percent=100, status="rate-limited"):
    reset = datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()
    windows = {name: {"status": "ok", "percent": 10, "resetsAt": reset}
               for name in ("rolling", "weekly", "monthly")}
    if full is not None:
        windows[full].update(status=status, percent=percent)
    return json.dumps({"usage": windows}).encode()


class UsageHandler(FakeHandler):
    def do_GET(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.calls.append(SimpleNamespace(
            method="GET", path=self.path, body=body,
            headers={key.lower(): value for key, value in self.headers.items()}))
        if self.path != "/zen/go/v1/usage":
            self.server.errors.append("usage must use the fixed upstream /usage path")
        try:
            reply = self.server.usage_replies.get_nowait()
        except queue.Empty:
            self.server.errors.append("unexpected usage refresh")
            reply = Reply(500, b"{}")
        reply.entered.set()
        while reply.hold is not None and not reply.hold.wait(0.01):
            if self.server.stopping.is_set():
                return
        self.close_connection = True
        if reply.drop:
            return
        try:
            self._respond(reply)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), UsageHandler)
    server.replies, server.usage_replies = queue.Queue(), queue.Queue()
    server.calls, server.errors = [], []
    server.stopping = threading.Event()
    server.url = f"http://127.0.0.1:{server.server_port}/zen/go/v1"
    with serving(server):
        try:
            yield server
        finally:
            server.stopping.set()
    assert not server.errors


@pytest.fixture
def shared_gateway(upstream):
    """A second server with an independent store object and the same state file."""
    from pbatch.llm_pool_gateway import PoolGateway

    with ExitStack() as stack:
        clients = []

        def make(settings):
            server = stack.enter_context(serving(PoolGateway(
                settings, port=0, upstream_url=upstream.url)))
            client = GatewayClient(server)
            clients.append(client)
            return client

        yield make
        for client in clients:
            client.idle()


def calls(upstream, method):
    return [call for call in upstream.calls if getattr(call, "method", "POST") == method]


def sequence(upstream):
    return [(getattr(call, "method", "POST"), call.headers["authorization"])
            for call in upstream.calls]


@pytest.mark.parametrize("window", ["rolling", "weekly", "monthly"])
def test_full_usage_skips_a_post_and_queries_b(gateway_factory, upstream, window):
    gateway = gateway_factory(usage_enabled=True)
    upstream.usage_replies.put(Reply(body=usage_body(window)))
    upstream.usage_replies.put(Reply(body=usage_body()))
    upstream.replies.put(Reply())
    result = gateway.request()
    assert result.status == 200 and result.body == OK
    assert sequence(upstream) == [("GET", "Bearer " + KEYS[0]),
                                  ("GET", "Bearer " + KEYS[1]), ("POST", "Bearer " + KEYS[1])]
    assert calls(upstream, "POST")[0].body == RAW
    assert all(call.path == "/zen/go/v1/usage" and call.body == b"" for call in calls(upstream, "GET"))
    gateway.idle()


def test_all_usage_exhausted_returns_429_without_inference(gateway_factory, upstream):
    gateway = gateway_factory(usage_enabled=True)
    upstream.usage_replies.put(Reply(body=usage_body("weekly")))
    upstream.usage_replies.put(Reply(body=usage_body("monthly")))
    result = gateway.request()
    assert result.status == 429
    assert json.loads(result.body)["error"]["code"] == "pool_unavailable"
    assert 3500 <= int(result.headers["retry-after"]) <= 3601
    assert len(calls(upstream, "GET")) == 2 and calls(upstream, "POST") == []


def test_fresh_cache_is_shared_by_gateway_instances(gateway_factory, shared_gateway, upstream):
    first = gateway_factory(groups=("shared",), usage_enabled=True, usage_max_age=60)
    upstream.usage_replies.put(Reply(body=usage_body()))
    upstream.replies.put(Reply())
    assert first.request().status == 200
    first.idle()
    second = shared_gateway(first.server.settings)
    upstream.replies.put(Reply())
    assert second.request().status == 200
    assert len(calls(upstream, "GET")) == 1 and len(calls(upstream, "POST")) == 2


@pytest.mark.parametrize("refresh_ok", [False, True])
def test_expired_available_cache_is_refreshed_before_post(gateway_factory, upstream, refresh_ok):
    gateway = gateway_factory(groups=("single",), usage_enabled=True, usage_max_age=1)
    upstream.usage_replies.put(Reply(body=usage_body()))
    upstream.replies.put(Reply())
    assert gateway.request().status == 200
    gateway.idle()
    threading.Event().wait(1.05)
    upstream.usage_replies.put(Reply(body=usage_body("rolling")) if refresh_ok else Reply(503, b"{}"))
    result = gateway.request()
    assert result.status == (429 if refresh_ok else 503)
    assert len(calls(upstream, "GET")) == 2 and len(calls(upstream, "POST")) == 1


def test_keys_in_one_quota_group_share_one_usage_refresh(gateway_factory, upstream):
    gateway = gateway_factory(groups=("shared", "shared", "independent"), usage_enabled=True)
    upstream.usage_replies.put(Reply(body=usage_body("rolling")))
    upstream.usage_replies.put(Reply(body=usage_body()))
    upstream.replies.put(Reply())
    assert gateway.request().status == 200
    assert sequence(upstream) == [("GET", "Bearer " + KEYS[0]),
                                  ("GET", "Bearer " + KEYS[2]), ("POST", "Bearer " + KEYS[2])]


def test_parallel_gateways_share_refresh_ticket(gateway_factory, shared_gateway, upstream):
    seed = gateway_factory(groups=("shared",), usage_enabled=True)
    config = replace(seed.server.settings, accounts=tuple(
        replace(account, max_concurrent=2) for account in seed.server.settings.accounts))
    first, second = shared_gateway(config), shared_gateway(config)
    release, started = threading.Event(), threading.Event()
    refresh = Reply(body=usage_body(), hold=release)
    upstream.usage_replies.put(refresh)
    upstream.replies.put(Reply())
    upstream.replies.put(Reply())

    def other_request():
        started.set()
        return second.request()

    with ThreadPoolExecutor(max_workers=2) as workers:
        a = workers.submit(first.request)
        try:
            assert refresh.entered.wait(3)
            b = workers.submit(other_request)
            assert started.wait(3)
            threading.Event().wait(0.15)
            assert len(calls(upstream, "GET")) == 1
            assert calls(upstream, "POST") == []
        finally:
            release.set()
        assert a.result(timeout=3).status == 200
        result = b.result(timeout=3)
        assert result.status in (200, 503)  # Waiting or failing closed while refresh is owned.
        if result.status == 503:
            assert json.loads(result.body)["error"]["code"] == "pool_usage_unavailable"
    first.idle()
    if result.status == 503:
        assert second.request().status == 200
    assert len(calls(upstream, "GET")) == 1


def invalid_usage(case):
    data = json.loads(usage_body())
    rolling = data["usage"]["rolling"]
    if case == "missing-window":
        del data["usage"]["weekly"]
    elif case == "extra-window":
        data["usage"]["daily"] = rolling.copy()
    elif case == "bad-status":
        rolling["status"] = "unknown"
    elif case == "bad-reset":
        rolling["resetsAt"] = "not-an-ISO-date"
    elif case == "full-but-ok":
        rolling["percent"] = 100
    elif case == "limited-but-not-full":
        rolling["status"] = "rate-limited"
    else:
        rolling["percent"] = {"boolean": True, "string": "10", "negative": -1,
                              "too-high": 101, "nan": float("nan")}[case]
    return json.dumps(data).encode()


@pytest.mark.parametrize("case", ["missing-window", "extra-window", "bad-status", "bad-reset",
                                  "full-but-ok", "limited-but-not-full", "boolean", "string",
                                  "negative", "too-high", "nan"])
def test_usage_schema_errors_fail_closed(gateway_factory, upstream, case):
    gateway = gateway_factory(groups=("single",), usage_enabled=True)
    upstream.usage_replies.put(Reply(body=invalid_usage(case)))
    result = gateway.request()
    assert result.status == 503
    assert json.loads(result.body)["error"]["code"] == "pool_usage_unavailable"
    assert len(calls(upstream, "GET")) == 1 and calls(upstream, "POST") == []


@pytest.mark.parametrize("mode", ["disconnect", "unauthorized", "server-error", "malformed"])
@pytest.mark.parametrize("fallback", [False, True])
def test_failed_usage_never_admits_unverified_group(gateway_factory, upstream, mode, fallback):
    gateway = gateway_factory(usage_enabled=True)
    bad = {"disconnect": Reply(drop=True), "unauthorized": Reply(401, b"{}"),
           "server-error": Reply(503, b"{}"), "malformed": Reply(body=b"not JSON")}
    upstream.usage_replies.put(bad[mode])
    upstream.usage_replies.put(Reply(body=usage_body()) if fallback else Reply(401, b"{}"))
    if fallback:
        upstream.replies.put(Reply())
    result = gateway.request()
    assert result.status == (200 if fallback else 503)
    if not fallback:
        assert json.loads(result.body)["error"]["code"] == "pool_usage_unavailable"
    assert len(calls(upstream, "GET")) == 2
    posts = calls(upstream, "POST")
    assert len(posts) == int(fallback)
    assert all(post.headers["authorization"] == "Bearer " + KEYS[1] for post in posts)


def test_post_quota_still_fails_over_after_available_usage(gateway_factory, upstream):
    gateway = gateway_factory(usage_enabled=True)
    for _ in range(2):
        upstream.usage_replies.put(Reply(body=usage_body()))
    error = {"type": "error", "error": {"type": "GoUsageLimitError",
             "message": "OpenCode Go usage limit exceeded."}, "metadata": {"limitName": "5 hour"}}
    upstream.replies.put(Reply(429, json.dumps(error).encode(), {"Retry-After": "120"}))
    upstream.replies.put(Reply())
    assert gateway.request().status == 200
    assert sequence(upstream) == [("GET", "Bearer " + KEYS[0]), ("POST", "Bearer " + KEYS[0]),
                                  ("GET", "Bearer " + KEYS[1]), ("POST", "Bearer " + KEYS[1])]
    assert all(post.body == RAW for post in calls(upstream, "POST"))


@pytest.mark.parametrize("status,expected", [(429, 429), (401, 401), (503, 502), (None, 502)])
def test_nonquota_post_failure_is_not_replayed_after_preflight(gateway_factory, upstream, status, expected):
    gateway = gateway_factory(usage_enabled=True)
    upstream.usage_replies.put(Reply(body=usage_body()))
    upstream.replies.put(Reply(status, b"{}") if status is not None else Reply(drop=True))
    assert gateway.request().status == expected
    assert sequence(upstream) == [("GET", "Bearer " + KEYS[0]), ("POST", "Bearer " + KEYS[0])]


def test_usage_get_strips_client_secrets_and_error_response_is_redacted(gateway_factory, upstream, capsys):
    gateway = gateway_factory(groups=("single",), usage_enabled=True)
    upstream.usage_replies.put(Reply(401, json.dumps({"error": KEYS[0] + TOKEN}).encode(),
                                   {"X-Debug-Key": KEYS[0], "Set-Cookie": KEYS[0]}))
    headers = {"Authorization": None, "x-api-key": TOKEN, "Cookie": "private-cookie",
               "Proxy-Authorization": TOKEN, "x-forwarded-authorization": TOKEN}
    result = gateway.request(headers=headers)
    assert result.status == 503 and calls(upstream, "POST") == []
    sent = calls(upstream, "GET")[0].headers
    assert sent["authorization"] == "Bearer " + KEYS[0]
    assert {"cookie", "x-api-key", "proxy-authorization", "x-forwarded-authorization"}.isdisjoint(sent)
    assert TOKEN not in json.dumps(sent) and "private-cookie" not in json.dumps(sent)
    logs = capsys.readouterr()
    public = (result.body.decode() + json.dumps(result.headers) + logs.out + logs.err
              + json.dumps(gateway.idle()) + json.dumps(gateway.server.store.events()))
    assert all(secret not in public for secret in (TOKEN, *KEYS))


@pytest.mark.parametrize("mode", ["stall", "drip"])
def test_slow_usage_has_absolute_query_deadline(gateway_factory, upstream, mode):
    gateway = gateway_factory(groups=("single",), usage_enabled=True, usage_timeout=0.3, request_timeout=2)
    release = threading.Event()
    reply = (Reply(body=usage_body(), hold=release) if mode == "stall" else
             Reply(body=b" ", chunks=((0.08, b" "),) * 30))
    upstream.usage_replies.put(reply)
    start = time.monotonic()
    try:
        result = gateway.request()
        elapsed = time.monotonic() - start
        assert result.status == 503
        assert json.loads(result.body)["error"]["code"] == "pool_usage_unavailable"
        assert 0.2 <= elapsed < 1.0, f"usage query exceeded its 0.3s deadline: {elapsed:.2f}s"
        assert len(calls(upstream, "GET")) == 1 and calls(upstream, "POST") == []
        gateway.idle(timeout=0.25)
    finally:
        release.set()


def test_usage_cannot_extend_absolute_inference_deadline(gateway_factory, upstream):
    gateway = gateway_factory(usage_enabled=True, usage_timeout=3, request_timeout=0.5)
    release = threading.Event()
    upstream.usage_replies.put(Reply(body=usage_body(), hold=release))
    start = time.monotonic()
    try:
        try:
            result = gateway.request()
            assert result.status in (502, 503, 504)
        except (http.client.RemoteDisconnected, ConnectionResetError):
            pass
        assert time.monotonic() - start < 1.1
        assert len(calls(upstream, "GET")) == 1 and calls(upstream, "POST") == []
        gateway.idle(timeout=0.25)
    finally:
        release.set()


def test_default_disabled_preserves_inference_without_usage(gateway_factory, upstream):
    gateway = gateway_factory()
    upstream.replies.put(Reply())
    result = gateway.request()
    assert result.status == 200 and result.body == OK
    assert sequence(upstream) == [("POST", "Bearer " + KEYS[0])]


def test_disabled_preflight_respects_persisted_full_usage(gateway_factory, shared_gateway, upstream):
    enabled = gateway_factory(usage_enabled=True)
    upstream.usage_replies.put(Reply(body=usage_body("monthly")))
    upstream.usage_replies.put(Reply(body=usage_body()))
    upstream.replies.put(Reply())
    assert enabled.request().status == 200
    enabled.idle()
    disabled = shared_gateway(replace(enabled.server.settings, usage_enabled=False))
    upstream.replies.put(Reply())
    assert disabled.request().status == 200
    assert len(calls(upstream, "GET")) == 2
    assert all(call.headers["authorization"] == "Bearer " + KEYS[1] for call in calls(upstream, "POST"))


def test_disabled_preflight_respects_operator_refresh(gateway_factory, upstream):
    from pbatch.llm_pool_usage_refresh import refresh_account

    gateway = gateway_factory()
    settings = gateway.server.settings
    upstream.usage_replies.put(Reply(body=usage_body("weekly")))
    assert refresh_account(settings, gateway.server.store, settings.accounts[0],
                           upstream_url=upstream.url)
    upstream.replies.put(Reply())
    assert gateway.request().status == 200
    assert sequence(upstream) == [("GET", "Bearer " + KEYS[0]), ("POST", "Bearer " + KEYS[1])]


@pytest.mark.parametrize("known_first", [False, True])
def test_mixed_unknown_and_exhausted_usage_returns_503(gateway_factory, upstream, known_first):
    gateway = gateway_factory(usage_enabled=True)
    answers = [Reply(body=usage_body("weekly")), Reply(503, b"{}")]
    for answer in answers if known_first else reversed(answers):
        upstream.usage_replies.put(answer)
    result = gateway.request()
    assert result.status == 503
    assert json.loads(result.body)["error"]["code"] == "pool_usage_unavailable"
    assert len(calls(upstream, "GET")) == 2 and calls(upstream, "POST") == []


@pytest.mark.parametrize("status", [302, 307])
def test_usage_redirect_is_not_followed(gateway_factory, upstream, status):
    gateway = gateway_factory(groups=("single",), usage_enabled=True)
    upstream.usage_replies.put(Reply(status, b"", {"Location": upstream.url + "/elsewhere"}))
    result = gateway.request()
    assert result.status == 503
    assert "location" not in result.headers
    assert len(calls(upstream, "GET")) == 1 and calls(upstream, "POST") == []


@pytest.mark.parametrize("headers,body,expected", [
    ({"Authorization": None}, RAW, 401),
    ({"x-opencode-session": None}, RAW, 400),
    ({}, b'{"model":"unconfigured"}', 400),
])
def test_invalid_inference_is_rejected_before_usage_get(gateway_factory, upstream, headers, body, expected):
    result = gateway_factory(usage_enabled=True).request(body, headers=headers)
    assert result.status == expected
    assert upstream.calls == []
