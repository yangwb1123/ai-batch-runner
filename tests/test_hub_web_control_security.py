"""Adversarial browser task contracts, with no real identities or providers."""

import http.client
import json
from dataclasses import replace
from email.message import Message
from types import SimpleNamespace

import pytest

from pbatch.hub_errors import HubAccessError
from pbatch.hub_web_control import _body_bytes, _write_binding
from test_hub_auth import signed
from test_hub_web import web_env
from test_hub_web_control import control, authenticated, submit, create_session, SCOPES
from test_hub_web_read_grants import readers


def test_tenant_metadata_policy_never_reads_task_content_or_writes(control, signed):
    call, server, store, _, _ = control
    server.auth = server.web.auth = readers(signed[2])
    cookie, access = authenticated(call)
    assert access["mode"] == "control" and not access["task_access"] and not access["scopes"]
    assert call("/web/api/instances", cookie=cookie)[0] == 200
    assert call("/web/api/session-operations", cookie=cookie)[0] == 403
    assert submit(call, cookie, access, "/web/api/instances/alpha/sessions", {"name": "wrong"})[0] == 403
    assert call("/web/api/sessions", cookie=cookie)[0] == 200
    assert not store.session_requests("team", "alpha", 1)


def test_membership_gate_denies_capability_projection_and_commands(control):
    call, server, store, _, _ = control
    cookie, access = authenticated(call)

    class Denied:
        def check(self, _principal):
            raise HubAccessError("membership_denied", "synthetic membership denial")

    server.service.identity = Denied()
    assert call("/web/api/access", cookie=cookie)[0] == 403
    assert submit(call, cookie, access, "/web/api/instances/alpha/sessions", {"name": "wrong"})[0] == 403
    assert not store.session_requests("team", "alpha", 1)


def test_cancel_scope_is_separate_from_session_write(control):
    call, server, store, _, _ = control
    cookie, access = authenticated(call)
    session = create_session(control, cookie, access)
    status, body = submit(call, cookie, access, f"/web/api/sessions/{session}/turns", {"prompt": "queued"})
    assert status == 202
    turn = body["data"]["turn_id"]
    server.auth.grants[0] = replace(server.auth.grants[0], scopes=frozenset(SCOPES - {"agent.turns:cancel"}))
    assert submit(call, cookie, access, f"/web/api/turns/{turn}/cancel", {})[0] == 403
    assert store.turn("team", turn)["state"] == "queued"


def test_logout_during_body_read_blocks_delayed_submission(control, monkeypatch):
    from pbatch import hub_web_control
    call, server, store, _, _ = control
    cookie, access = authenticated(call)
    original = hub_web_control._body

    def logout(handler):
        result = original(handler)
        server.web.clear(cookie.partition("=")[2])
        return result

    monkeypatch.setattr(hub_web_control, "_body", logout)
    assert submit(call, cookie, access, "/web/api/instances/alpha/sessions", {"name": "wrong"})[0] == 401
    assert not store.session_requests("team", "alpha", 1)


@pytest.mark.parametrize("name", ["Origin", "Host", "Content-Length", "Idempotency-Key", "X-ABR-Context", "Content-Type"])
def test_duplicate_security_headers_are_rejected(control, name):
    call, server, store, _, _ = control
    cookie, access = authenticated(call)
    headers = {"Host": "abr.test", "Origin": "https://abr.test", "Cookie": cookie,
               "Content-Type": "application/json", "Content-Length": "12",
               "X-ABR-Context": access["context"], "Idempotency-Key": "request-0000000001"}
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.putrequest("POST", "/web/api/instances/alpha/sessions", skip_host=True)
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.putheader(name, headers[name])
        connection.endheaders(b'{"name":"x"}')
        response = connection.getresponse()
        assert response.status in {400, 403, 415}
        response.read()
    finally:
        connection.close()
    assert not store.session_requests("team", "alpha", 1)


@pytest.mark.parametrize("path", ["/web/api/gateway/register", "/web/api/compute/tasks", "/web/api/files/x", "/web/api/sessions/a/unknown"])
def test_unlisted_control_routes_stay_closed(control, path):
    call, *_ = control
    cookie, _ = authenticated(call)
    assert call(path, cookie=cookie)[0] == 404


def test_continuous_body_bytes_do_not_extend_the_total_deadline(monkeypatch):
    from pbatch import hub_web_control
    now, timeouts = [0.0], []

    def read(_size):
        now[0] += 2
        return b"x"

    connection = SimpleNamespace(gettimeout=lambda: 15, settimeout=timeouts.append)
    handler = SimpleNamespace(connection=connection, rfile=SimpleNamespace(read1=read))
    monkeypatch.setattr(hub_web_control.time, "monotonic", lambda: now[0])
    with pytest.raises(TimeoutError, match="deadline"):
        _body_bytes(handler, 4)
    assert timeouts == [5, 3, 1, 15]


@pytest.mark.parametrize("length", ["", "short", "a" * 129])
def test_missing_or_unbounded_idempotency_key_fails_before_dispatch(length):
    from pbatch.hub_web import HubWeb
    headers = Message()
    headers["X-ABR-Context"] = HubWeb.context("k" * 43)
    if length:
        headers["Idempotency-Key"] = length
    with pytest.raises(ValueError):
        _write_binding(SimpleNamespace(headers=headers), HubWeb, "k" * 43)
