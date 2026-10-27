"""Real local HTTP facade; synthetic issuer, real SDK and JWT verification."""

from __future__ import annotations

import http.client
import json
import threading
from urllib.parse import urlencode

import pytest

from pbatch.hub_errors import HubAccessError
from pbatch.hub_http import HubHTTPServer, _settings
from pbatch.hub_store import HubStore
from pbatch.hub_web_http import COOKIE
from test_hub_web import callback, web_env
from test_hub_auth import signed


@pytest.fixture
def browser(web_env, tmp_path):
    web, auth, *_ = web_env
    store = HubStore(tmp_path / "web.sqlite3")
    for instance in ("alpha", "private-beta"):
        registered = store.register_instance("team", instance, {"boot_id": "boot-" + instance})
        store.upsert_session("team", instance, registered["generation"], {
            "local_session_id": "session", "name": "<script>untrusted</script>", "status": "idle"})
    server = HubHTTPServer(("127.0.0.1", 0), store, auth,
                           allowed_origins=[web.origin], web=web)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, *, method="GET", cookie="", headers=None):
        values = {"Host": web.host, **(headers or {})}
        if cookie:
            values["Cookie"] = cookie
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, headers=values)
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    yield call, server, web_env
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    store.close()


def sign_in(call):
    status, headers, body = call("/auth/login", method="POST", headers={"Origin": "https://abr.test"})
    assert status == 303 and not body
    pending = headers["Set-Cookie"].split(";", 1)[0]
    values = callback(headers["Location"])
    path = "/auth/callback?" + urlencode(values)
    status, headers, body = call(path, cookie=pending)
    assert status == 303 and not body and headers["Location"] == "/"
    return headers["Set-Cookie"].split(";", 1)[0], pending, path


def test_login_reads_are_granted_filtered_and_not_bearer_api_auth(browser, capsys):
    call, _, (_, _, transport, _, _) = browser
    assert call("/web/api/instances")[0] == 401
    cookie, pending, path = sign_in(call)
    assert cookie != pending
    status, headers, body = call("/web/api/instances", cookie=cookie)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert [row["instance_id"] for row in json.loads(body)["data"]] == ["alpha"]
    status, _, body = call("/web/api/sessions", cookie=cookie)
    assert status == 200
    assert {row["instance_id"] for row in json.loads(body)["data"]} == {"alpha"}
    assert call("/web/api/sessions?instance_id=private-beta", cookie=cookie)[0] == 403
    assert call("/api/v1/agent/instances", cookie=cookie)[0] == 401
    assert call(path, cookie=pending)[0] == 401
    assert call("/api/v1/agent/instances", headers={"Authorization": "Bearer " + transport.token()})[0] == 200
    captured = capsys.readouterr()
    assert "test-code" not in captured.out + captured.err
    assert transport.token().encode() not in body


def test_secure_cookie_csp_and_credential_free_page(browser):
    call, _, _ = browser
    status, headers, body = call("/")
    assert status == 200
    policy = headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'self' https://identity.test" in policy  # allow only the pinned login redirect
    assert "unsafe-inline" not in policy
    assert headers["Referrer-Policy"] == "same-origin"  # native forms must send a non-null Origin
    assert b"localStorage" not in body and b"sessionStorage" not in body and b"innerHTML" not in body
    assert b"access_token" not in body and b"client_secret" not in body
    status, headers, _ = call("/auth/login", method="POST", headers={"Origin": "https://abr.test"})
    assert status == 303
    assert headers["Referrer-Policy"] == "no-referrer"  # never forward callback/login parameters
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(COOKIE + "=")
    for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=600"):
        assert flag in cookie
    assert "Domain=" not in cookie


@pytest.mark.parametrize("path", ["/auth/login", "/auth/logout"])
@pytest.mark.parametrize("origin", [None, "null", "https://evil.test"])
def test_csrf_requires_exact_origin_not_forwarded_headers(browser, path, origin):
    call, _, _ = browser
    headers = {"X-Forwarded-Host": "abr.test", "X-Forwarded-Proto": "https"}
    if origin is not None:
        headers["Origin"] = origin
    assert call(path, method="POST", headers=headers)[0] == 403


@pytest.mark.parametrize("path,method,headers,status", [
    ("/", "GET", {"Host": "evil.test"}, 403),
    ("/", "GET", {"Authorization": "Bearer ignored"}, 400),
    ("/auth/login", "GET", {}, 405),
    ("/auth/logout", "GET", {}, 405),
    ("/web/api/instances", "POST", {"Origin": "https://abr.test"}, 405),
    ("/web/api/instances", "OPTIONS", {}, 405),
    ("/web/api/sessions", "DELETE", {}, 405),
    ("/web/api/gateway/register", "POST", {"Origin": "https://abr.test"}, 404),
    ("/?access_token=secret", "GET", {}, 400),
    ("/web/api/instances?limit=101", "GET", {}, 400),
    ("/web/api/instances?limit=1&limit=2", "GET", {}, 400),
    ("/web/api/sessions?token=secret", "GET", {}, 400),
    ("/", "GET", {"Transfer-Encoding": "chunked"}, 400),
])
def test_browser_boundary_is_closed(browser, path, method, headers, status):
    assert browser[0](path, method=method, headers=headers)[0] == status


def test_duplicate_cookie_and_callback_parameters_are_rejected(browser):
    call, _, _ = browser
    cookie, pending, path = sign_in(call)
    assert call("/web/api/instances", cookie=cookie + "; " + cookie)[0] == 400
    assert call(path + "&code=duplicate", cookie=pending)[0] == 400
    assert call("/auth/callback?state=missing", cookie=cookie)[0] == 401
    assert call("/" + "?x=" + "a" * 8192)[0] == 400


def test_local_logout_does_not_call_issuer_or_cancel_tasks(browser):
    call, _, (_, _, transport, _, _) = browser
    cookie, _, _ = sign_in(call)
    status, headers, _ = call("/auth/logout", method="POST", cookie=cookie,
                              headers={"Origin": "https://abr.test"})
    assert status == 303 and "Max-Age=0" in headers["Set-Cookie"]
    assert call("/web/api/instances", cookie=cookie)[0] == 401
    assert len(transport.calls) == 1


def test_identity_gate_still_owns_membership_admission(browser):
    call, server, _ = browser
    cookie, _, _ = sign_in(call)

    class DenyMembership:
        def check(self, principal):
            assert principal.tenant_id == "team"
            raise HubAccessError("membership_denied", "membership unavailable")

    server.service.identity = DenyMembership()
    assert call("/web/api/sessions", cookie=cookie)[0] == 403


def test_opt_in_config_is_loaded_without_silently_ignoring_bad_fields(tmp_path, web_env):
    _, _, _, settings, _ = web_env
    path = tmp_path / "config.json"
    path.write_text(json.dumps(settings))
    assert _settings(str(path))["web"]["public_origin"] == "https://abr.test"
    settings["unknown"] = True
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="unknown"):
        _settings(str(path))
