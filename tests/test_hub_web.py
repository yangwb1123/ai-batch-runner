"""Real Snaplink SDK + signed JWT contracts; issuer transport is synthetic."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from snaplink_sso import Snaplink

from pbatch.hub_errors import HubAccessError
from pbatch.hub_web import HubWeb
from test_hub_auth import signed


class TokenEndpoint:
    def __init__(self, token):
        self.token = token
        self.calls = []
        self.reply = None

    def send(self, request):
        self.calls.append(request)
        assert request["url"] == "https://identity.test/token"
        assert request["method"] == "POST"
        body = self.reply or {"access_token": self.token(), "token_type": "Bearer"}
        return {"status": 200, "body": json.dumps(body).encode()}


@pytest.fixture
def web_env(signed):
    auth, token, verifier, *_ = signed
    auth.grants = [replace(grant, scopes=frozenset({
        "agent.instances:read", "agent.sessions:read"})) for grant in auth.grants]
    transport = TokenEndpoint(lambda: token(scope="agent.instances:read agent.sessions:read"))
    settings = {
        "auth": {"mode": "jwt", "issuer": verifier.issuer, "audience": verifier.audience},
        "allowed_origins": ["https://abr.test"],
        "web": {"public_origin": "https://abr.test", "client_id": "console", "tenant_id": "team"},
    }
    clock = [0.0]
    web = HubWeb(settings, auth, transport=transport, clock=lambda: clock[0])
    return web, auth, transport, settings, clock


def callback(url):
    query = parse_qs(urlsplit(url).query)
    return {"state": query["state"][0], "iss": "https://identity.test", "code": "test-code"}


def logged_in(web):
    pending, url = web.begin("")
    return web.complete(pending, callback(url))


def test_real_sdk_s256_code_exchange_and_cookie_rotation(web_env):
    web, _, transport, _, _ = web_env
    pending, url = web.begin("")
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://identity.test/login/?")
    assert query["resource"] == ["agent-hub"]
    assert query["scope"] == ["openid agent.instances:read agent.sessions:read"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://abr.test/auth/callback"]
    session = web.complete(pending, callback(url))
    assert session != pending
    form = parse_qs(transport.calls[0]["body"].decode())
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest())
    assert query["code_challenge"] == [challenge.rstrip(b"=").decode()]
    assert form["grant_type"] == ["authorization_code"]
    assert "client_secret" not in form
    assert web.principal(session).instances == {"alpha"}
    with pytest.raises(HubAccessError):
        web.principal(pending)
    with pytest.raises(HubAccessError):
        web.complete(pending, callback(url))
    assert len(transport.calls) == 1


@pytest.mark.parametrize("change", [{"state": "other"}, {"iss": "https://evil.test"},
                                   {"code": ""}, {"error": "access_denied"}])
def test_sdk_rejects_bad_callbacks_without_exchange(web_env, change):
    web, _, transport, _, _ = web_env
    pending, url = web.begin("")
    values = {**callback(url), **change}
    with pytest.raises(HubAccessError):
        web.complete(pending, values)
    assert not transport.calls
    with pytest.raises(HubAccessError):
        web.complete(pending, callback(url))


def test_login_transactions_are_bound_to_separate_browsers(web_env):
    web, _, transport, _, _ = web_env
    first, first_url = web.begin("")
    second, second_url = web.begin("")
    with pytest.raises(HubAccessError):
        web.complete(first, callback(second_url))
    assert not transport.calls
    session = web.complete(second, callback(second_url))
    assert web.principal(session).tenant_id == "team"
    assert callback(first_url)["state"] != callback(second_url)["state"]


@pytest.mark.parametrize("claims", [{"aud": "other"}, {"tenant_id": "other"},
                                   {"sub": "ungranted"}, {"exp": 1},
                                   {"client_id": "other"}, {"scope": "agent.sessions:read"}])
def test_login_does_not_replace_resource_authorization(web_env, signed, claims):
    web, _, transport, _, _ = web_env
    _, token, *_ = signed
    transport.token = lambda: token(**claims)
    with pytest.raises(HubAccessError):
        logged_in(web)


def test_bound_web_tenant_client_cannot_adopt_another_valid_grant(web_env):
    web, auth, _, _, _ = web_env
    session = logged_in(web)
    auth.grants[0] = replace(auth.grants[0], kind="gateway")
    with pytest.raises(HubAccessError):
        web.principal(session)


def test_each_read_reauthenticates_and_clear_is_local_only(web_env):
    web, auth, transport, _, _ = web_env
    session = logged_in(web)
    assert web.principal(session).subject == "alice"
    auth.grants.clear()
    with pytest.raises(HubAccessError):
        web.principal(session)
    web.clear(session)
    assert len(transport.calls) == 1  # no global SSO logout or silent refresh


def test_absolute_session_and_transaction_ttl_and_capacity(web_env):
    web, _, _, _, clock = web_env
    pending, url = web.begin("")
    clock[0] = 601
    with pytest.raises(HubAccessError):
        web.complete(pending, callback(url))
    session = logged_in(web)
    clock[0] += 1801
    with pytest.raises(HubAccessError):
        web.principal(session)
    for _ in range(256):
        web.begin("")
    with pytest.raises(HubAccessError) as error:
        web.begin("")
    assert error.value.status == 429
    clock[0] += 601
    assert web.begin("")[0]


@pytest.mark.parametrize("reply", [{}, {"access_token": "x", "token_type": "DPoP"},
                                  {"access_token": "x"}, {"error": "SECRET"}])
def test_bad_sdk_token_response_never_creates_authenticated_session(web_env, reply):
    web, _, transport, _, _ = web_env
    transport.reply = reply or {"access_token": None}
    with pytest.raises(HubAccessError) as error:
        logged_in(web)
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("change", [{"public_origin": "http://abr.test"},
                                   {"public_origin": "https://abr.test/"},
                                   {"public_origin": "https://user:pw@abr.test"},
                                   {"public_origin": "https://abr.test?bad"},
                                   {"client_id": ""}, {"tenant_id": ""},
                                   {"client_secret": "never-accepted"}])
def test_invalid_web_config_fails_closed(web_env, change):
    _, auth, _, settings, _ = web_env
    settings["web"].update(change)
    with pytest.raises(ValueError):
        HubWeb(settings, auth)


def test_web_requires_jwt_exact_origin_and_real_sdk(web_env, monkeypatch):
    _, auth, _, settings, _ = web_env
    assert Snaplink.__module__ == "snaplink_sso.hosted_login"
    settings["allowed_origins"] = []
    with pytest.raises(ValueError, match="allowed_origins"):
        HubWeb(settings, auth)
    settings["allowed_origins"] = ["https://abr.test"]
    settings["auth"]["mode"] = "development"
    with pytest.raises(ValueError, match="jwt"):
        HubWeb(settings, auth)
    settings["auth"]["mode"] = "jwt"
    monkeypatch.setitem(sys.modules, "snaplink_sso", None)
    with pytest.raises(ValueError, match=r"abr\[web\]"):
        HubWeb(settings, auth)


@pytest.mark.parametrize("binding", [{"tenant_id": "other"}, {"client_id": "other"}])
def test_another_valid_granted_identity_is_not_the_configured_web_binding(web_env, signed, binding):
    web, auth, transport, _, _ = web_env
    _, token, *_ = signed
    auth.grants[0] = replace(auth.grants[0], **binding)
    transport.token = lambda: token(scope="agent.instances:read agent.sessions:read", **binding)
    assert auth.authenticate("Bearer " + transport.token()).kind == "user"
    with pytest.raises(HubAccessError, match="binding mismatch"):
        logged_in(web)


def test_logout_during_verification_cannot_restore_local_session(web_env, monkeypatch):
    web, auth, _, _, _ = web_env
    session = logged_in(web)
    authenticate = auth.authenticate

    def clear_then_verify(header):
        web.clear(session)
        return authenticate(header)

    monkeypatch.setattr(auth, "authenticate", clear_then_verify)
    with pytest.raises(HubAccessError, match="ended"):
        web.principal(session)


@pytest.mark.parametrize("issuer", ["https://bad'host.test", "https://bad;host.test"])
def test_issuer_cannot_inject_a_csp_source(web_env, issuer):
    _, auth, _, settings, _ = web_env
    settings["auth"]["issuer"] = issuer
    with pytest.raises(ValueError, match="CSP-safe"):
        HubWeb(settings, auth)


def test_shutdown_during_exchange_cannot_insert_a_new_session(web_env):
    web, _, transport, _, _ = web_env
    send = transport.send

    def close_then_exchange(request):
        web.close()
        return send(request)

    transport.send = close_then_exchange
    with pytest.raises(HubAccessError, match="closed"):
        logged_in(web)
    assert len(transport.calls) == 1


def test_concurrent_callback_is_consumed_before_exchange(web_env):
    import threading
    web, _, transport, _, _ = web_env
    pending, url = web.begin("")
    entered, release = threading.Event(), threading.Event()
    send, result = transport.send, []

    def blocked(request):
        entered.set()
        assert release.wait(3)
        return send(request)

    transport.send = blocked
    thread = threading.Thread(target=lambda: result.append(web.complete(pending, callback(url))))
    thread.start()
    try:
        assert entered.wait(3)
        with pytest.raises(HubAccessError):
            web.complete(pending, callback(url))
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and len(result) == 1 and len(transport.calls) == 1
