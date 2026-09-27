"""Admission, concurrency, and absolute-deadline gateway regressions."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import threading
import time

import pytest

from test_llm_pool_gateway import (
    KEYS, MODELS, RAW, TOKEN, Reply, gateway_factory, upstream,
)


pytestmark = pytest.mark.scenario


@pytest.mark.parametrize("headers,extra,expected", [
    ({"Authorization": None}, (), 401),
    ({"Authorization": "Bearer wrong-test-token-12345"}, (), 401),
    ({"Authorization": "Basic " + TOKEN}, (), 401),
    ({"Authorization": None, "x-api-key": "wrong-test-token-12345"}, (), 401),
    ({"x-api-key": "wrong-test-token-12345"}, (), 401),
    ({}, (("Authorization", "Bearer " + TOKEN),), 401),
    ({"Authorization": None, "x-api-key": TOKEN}, (("x-api-key", TOKEN),), 401),
    ({"Origin": "https://untrusted.invalid"}, (), 403),
    ({"x-opencode-session": None}, (), 400),
    ({"x-opencode-session": " "}, (), 400),
    ({"x-opencode-session": "s" * 513}, (), 400),
    ({}, (("x-opencode-session", "second-session"),), 400),
])
def test_invalid_auth_or_session_is_rejected_before_upstream(
        gateway_factory, upstream, headers, extra, expected):
    gateway = gateway_factory()
    result = gateway.request(headers=headers, extra=extra)
    assert result.status == expected
    assert upstream.calls == []
    assert gateway.idle()["in_flight"] == 0


@pytest.mark.parametrize("body,path", [
    (b'{"model":"unconfigured"}', "/v1/chat/completions"),
    (b'{"model":"message-model"}', "/v1/chat/completions"),
    (b'{"model":"chat-model"}', "/v1/messages"),
    (b'{"messages":[]}', "/v1/chat/completions"),
    (b'{"model":123}', "/v1/chat/completions"),
    (b'{"model":"chat-model","stream":"true"}', "/v1/chat/completions"),
    (b'{"model":"chat-model","model":"chat-model"}', "/v1/chat/completions"),
    (b'{"model":"chat-model","temperature":NaN}', "/v1/chat/completions"),
    (b'{"model":"chat-model","temperature":Infinity}', "/v1/chat/completions"),
    (b'[]', "/v1/chat/completions"),
    (b'{broken', "/v1/chat/completions"),
    (b'\xff', "/v1/chat/completions"),
])
def test_invalid_json_model_or_protocol_is_rejected_before_upstream(gateway_factory, upstream, body, path):
    result = gateway_factory().request(body, path=path)
    assert result.status == 400
    assert upstream.calls == []


@pytest.mark.parametrize("path", [
    "/v1/embeddings", "/chat/completions", "/v1/chat/completions?model=chat-model",
    "/v1/chat/completions/", "/v1/../chat/completions", "/v1/%63hat/completions",
    "http://127.0.0.1/v1/chat/completions",
])
def test_unknown_or_noncanonical_path_is_rejected_before_upstream(gateway_factory, upstream, path):
    assert gateway_factory().request(path=path).status == 404
    assert upstream.calls == []


@pytest.mark.parametrize("headers,extra,body", [
    ({"Content-Length": None}, (), RAW),
    ({"Content-Length": "0"}, (), b""),
    ({"Content-Length": "-1"}, (), b""),
    ({"Content-Length": str(2 * 1024 * 1024 + 1)}, (), b""),
    ({"Content-Length": "99999999999999999999999999999"}, (), b""),
    ({}, (("Content-Length", str(len(RAW))),), RAW),
    ({"Content-Length": None, "Transfer-Encoding": "chunked"}, (), b"0\r\n\r\n"),
    ({"Transfer-Encoding": "chunked"}, (), RAW),
    ({"Content-Type": "text/plain"}, (), RAW),
])
def test_unsafe_framing_and_oversize_are_rejected_without_reading_body(
        gateway_factory, upstream, headers, extra, body):
    result = gateway_factory().request(body, headers=headers, extra=extra)
    assert result.status in (400, 413)
    assert upstream.calls == []


@pytest.mark.parametrize("fields", [
    {"previous_response_id": "resp-on-account-a"},
    {"conversation": "conv-on-account-a"},
    {"background": True},
    {"input": [{"type": "item_reference", "id": "item-on-account-a"}]},
])
def test_responses_rejects_account_bound_history(gateway_factory, upstream, fields):
    body = json.dumps({"model": "response-model", **fields}).encode()
    result = gateway_factory().request(body, path="/v1/responses")
    assert result.status == 400
    assert upstream.calls == []


def test_models_are_static_and_require_authentication(gateway_factory, upstream):
    gateway = gateway_factory()
    denied = gateway.request(b"", method="GET", path="/v1/models", headers={"Authorization": None})
    assert denied.status == 401
    result = gateway.request(b"", method="GET", path="/v1/models")
    assert result.status == 200
    assert {entry["id"] for entry in json.loads(result.body)["data"]} == set(MODELS)
    assert gateway.request(b"", method="GET", path="/v1/status").status == 404
    assert upstream.calls == []
    assert all(secret.encode() not in result.body for secret in (TOKEN, *KEYS))


def test_concurrent_requests_respect_capacity_and_release_leases(gateway_factory, upstream):
    gateway = gateway_factory()
    release = threading.Event()
    first, second = Reply(hold=release), Reply(hold=release)
    upstream.replies.put(first)
    upstream.replies.put(second)
    with ThreadPoolExecutor(max_workers=2) as workers:
        a = workers.submit(gateway.request)
        b = workers.submit(gateway.request)
        try:
            assert first.entered.wait(3) and second.entered.wait(3)
            assert gateway.server.store.status(gateway.server.settings.accounts)["in_flight"] == 2
            assert gateway.request().status == 429
            assert len(upstream.calls) == 2
            assert {call.headers["authorization"] for call in upstream.calls} == {
                "Bearer " + KEYS[0], "Bearer " + KEYS[1]}
        finally:
            release.set()
        assert a.result(timeout=3).status == b.result(timeout=3).status == 200
    gateway.idle()
    upstream.replies.put(Reply())
    assert gateway.request().status == 200
    assert upstream.calls[-1].headers["authorization"] == "Bearer " + KEYS[0]


@pytest.mark.parametrize("mode", ["drip", "stall_after_progress"])
def test_stream_absolute_deadline_releases_lease_without_retry(gateway_factory, upstream, mode):
    gateway = gateway_factory(request_timeout=1)
    first = b'data: {"delta":"first"}\n\n'
    tick = b': heartbeat\n\n'
    chunks = ((0.1, tick),) * 40 if mode == "drip" else ((0.7, tick), (4, b"data: [DONE]\n\n"))
    upstream.replies.put(Reply(body=first, headers={"Content-Type": "text/event-stream"}, chunks=chunks))
    start = time.monotonic()
    connection, response = gateway.open(b'{"model":"chat-model","stream":true}')
    try:
        assert response.status == 200
        body = response.read()
    finally:
        response.close()
        connection.close()
    elapsed = time.monotonic() - start
    assert body.startswith(first) and b"[DONE]" not in body
    assert 0.7 <= elapsed < 1.6, f"stream exceeded absolute 1s deadline: {elapsed:.2f}s"
    assert len(upstream.calls) == 1
    gateway.idle(timeout=0.25)
    events = gateway.server.store.events()
    assert any(event["kind"] == "service" for event in events)
    assert not any(event["kind"] == "success" for event in events)
    upstream.replies.put(Reply())
    assert gateway.request().status == 200


def test_timeout_before_response_is_not_replayed_and_releases_lease(gateway_factory, upstream):
    gateway = gateway_factory(request_timeout=1)
    release = threading.Event()
    upstream.replies.put(Reply(hold=release))
    start = time.monotonic()
    try:
        try:
            result = gateway.request()
            assert result.status in (502, 504)
        except (http.client.RemoteDisconnected, ConnectionResetError):
            pass  # Closing the client socket at the hard deadline is also valid.
        assert time.monotonic() - start < 1.6
        assert len(upstream.calls) == 1
        gateway.idle(timeout=0.25)
        events = gateway.server.store.events()
        assert any(event["kind"] == "service" for event in events)
        assert not any(event["kind"] == "success" for event in events)
    finally:
        release.set()


def test_identical_provider_key_cannot_bypass_group_boundaries(gateway_factory, upstream):
    with pytest.raises(ValueError, match="identical credentials"):
        gateway_factory(keys=(KEYS[0], KEYS[0]))
    assert upstream.calls == []


def test_local_token_cannot_be_used_as_provider_key(gateway_factory, upstream):
    with pytest.raises(ValueError, match="different from provider"):
        gateway_factory(keys=(TOKEN, KEYS[1]))
    assert upstream.calls == []
