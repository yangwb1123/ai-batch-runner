"""Cookie task control delegates to the real Hub; issuer/Agent identities are synthetic."""

import http.client
import json
import threading
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore
from pbatch.hub_web import HubWeb
from test_hub_auth import signed
from test_hub_web import web_env
from test_hub_web_http import sign_in

SCOPES = {"agent.instances:read", "agent.sessions:read", "agent.sessions:write", "agent.turns:cancel"}


@pytest.fixture
def control(web_env, signed, tmp_path):
    old, auth, transport, settings, _ = web_env
    old.close()
    settings["web"]["control"] = True
    auth.grants = [replace(grant, scopes=frozenset(SCOPES)) for grant in auth.grants]
    transport.token = lambda: signed[1](scope=" ".join(sorted(SCOPES)))
    web = HubWeb(settings, auth, transport=transport)
    store = HubStore(tmp_path / "control.sqlite3")
    generations = {}
    for instance in ("alpha", "beta"):
        result = store.register_instance("team", instance, {"boot_id": instance,
            "project_id": "fixed-project", "session_capacity": 4, "session_close_supported": True})
        generations[instance] = result["generation"]
    server = HubHTTPServer(("127.0.0.1", 0), store, auth, web=web, allowed_origins=[web.origin])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, *, method="GET", cookie="", headers=None, body=None):
        values = {"Host": web.host, **(headers or {})}
        if cookie:
            values["Cookie"] = cookie
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, headers=values, body=body)
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    try:
        yield call, server, store, generations, transport
    finally:
        server.shutdown()
        server.server_close()
        web.close()
        thread.join(timeout=5)
        store.close()


def authenticated(call):
    cookie = sign_in(call)[0]
    status, _, body = call("/web/api/access", cookie=cookie)
    assert status == 200
    access = json.loads(body)["data"]
    return cookie, access


def submit(call, cookie, access, path, body, key="operation-0000000001", **headers):
    values = {"Origin": "https://abr.test", "Content-Type": "application/json",
              "Idempotency-Key": key, "X-ABR-Context": access["context"], **headers}
    status, _, raw = call(path, method="POST", cookie=cookie, headers=values,
                          body=json.dumps(body).encode())
    return status, json.loads(raw)


def create_session(control, cookie, access):
    call, _, store, generations, _ = control
    status, result = submit(call, cookie, access, "/web/api/instances/alpha/sessions", {"name": "task"})
    assert status == 202 and result["data"]["status"] == "queued"
    request = result["data"]
    created = store.report_session_request("team", "alpha", generations["alpha"],
        {"request_id": request["request_id"], "status": "created"})
    return created["session_id"]


def test_sdk_requests_control_scopes_only_when_opted_in(control):
    call, server, *_ = control
    _, url = server.web.begin("")
    assert set(parse_qs(urlsplit(url).query)["scope"][0].split()) == SCOPES | {"openid"}
    cookie, access = authenticated(call)
    assert access["task_access"] is True and set(access["scopes"]) == SCOPES
    assert access["mode"] == "control" and len(access["context"]) == 64
    assert call("/api/v1/agent/instances", cookie=cookie)[0] == 401


def test_create_send_events_cancel_and_close_share_real_hub_ledger(control):
    call, _, store, generations, _ = control
    cookie, access = authenticated(call)
    session = create_session(control, cookie, access)
    path = f"/web/api/sessions/{session}/turns"
    status, result = submit(call, cookie, access, path, {"prompt": "inspect only"}, "turn-000000000001")
    assert status == 202 and result["data"]["state"] == "queued"
    turn = result["data"]["turn_id"]
    assert submit(call, cookie, access, path, {"prompt": "inspect only"}, "turn-000000000001")[1] == result
    assert len(store.commands("team", "alpha", generations["alpha"])) == 1
    status, _, body = call(f"/web/api/sessions/{session}/events?limit=50&after=0", cookie=cookie)
    assert status == 200 and json.loads(body)["data"][0]["turn_id"] == turn
    status, result = submit(call, cookie, access, f"/web/api/turns/{turn}/cancel", {})
    assert status == 202 and result["data"]["state"] == "cancelled"
    status, result = submit(call, cookie, access, f"/web/api/sessions/{session}/close", {})
    assert status == 202 and result["data"]["status"] == "queued"
    assert store.session("team", session)["status"] == "closing"
    status, _, body = call("/web/api/session-operations?instance_id=alpha&limit=20", cookie=cookie)
    assert status == 200 and {item["operation"] for item in json.loads(body)["data"]} == {"create", "close"}


def test_instance_scope_revocation_and_membership_still_block_control(control):
    call, server, store, _, _ = control
    cookie, access = authenticated(call)
    assert submit(call, cookie, access, "/web/api/instances/beta/sessions", {"name": "wrong"})[0] == 403
    assert not store.session_requests("team", "beta", 1)
    server.auth.grants[0] = replace(server.auth.grants[0], scopes=frozenset(SCOPES - {"agent.sessions:write"}))
    assert submit(call, cookie, access, "/web/api/instances/alpha/sessions", {"name": "wrong"})[0] == 403
    assert not store.session_requests("team", "alpha", 1)


def test_changed_browser_identity_cannot_replay_a_previous_context(control):
    call, *_ = control
    cookie, access = authenticated(call)
    other, newer = authenticated(call)
    assert access["context"] != newer["context"]
    assert submit(call, other, access, "/web/api/instances/alpha/sessions", {"name": "wrong"})[0] == 403
    assert submit(call, cookie, access, "/web/api/instances/alpha/sessions", {"name": "right"})[0] == 202


@pytest.mark.parametrize("headers", [{"Origin": "null"}, {"Origin": "https://evil.test"},
    {"Host": "evil.test"}, {"Authorization": "Bearer ignored"}, {"X-ABR-Context": ""}])
def test_write_transport_rejections_have_no_side_effects(control, headers):
    call, _, store, _, _ = control
    cookie, access = authenticated(call)
    assert submit(call, cookie, access, "/web/api/instances/alpha/sessions", {"name": "wrong"}, **headers)[0] in {400, 403}
    assert not store.session_requests("team", "alpha", 1)


@pytest.mark.parametrize("path,body", [
    ("/web/api/instances/alpha/sessions?limit=1", {"name": "wrong"}),
    ("/web/api/instances/alpha/sessions", {"name": "wrong", "cwd": "/other/project"}),
    ("/web/api/instances/alpha%2fbeta/sessions", {"name": "wrong"}),
    ("/web/api/instances/%ff/sessions", {"name": "wrong"}),
])
def test_control_rejects_query_path_and_workspace_injection(control, path, body):
    call, _, store, _, _ = control
    cookie, access = authenticated(call)
    assert submit(call, cookie, access, path, body)[0] == 400
    assert not store.session_requests("team", "alpha", 1)


@pytest.mark.parametrize("raw", [b'{"name":"a","name":"b"}', b'[]', b'{"name":NaN}', b'\xff', b'x' * 65537],
                         ids=["duplicate", "array", "nonfinite", "utf8", "oversize"])
def test_control_json_is_bounded_strict_and_never_published(control, raw):
    call, _, store, _, _ = control
    cookie, access = authenticated(call)
    headers = {"Origin": "https://abr.test", "Content-Type": "application/json",
               "X-ABR-Context": access["context"], "Idempotency-Key": "request-0000000001"}
    assert call("/web/api/instances/alpha/sessions", method="POST", cookie=cookie,
                headers=headers, body=raw)[0] in {400, 413}
    assert not store.session_requests("team", "alpha", 1)


def test_default_read_only_profile_cannot_become_executable(control):
    call, server, store, _, _ = control
    server.web.control = False
    cookie, access = authenticated(call)
    assert access["task_access"] is False and access["mode"] == "read_only"
    assert not access["scopes"]
    assert submit(call, cookie, access, "/web/api/instances/alpha/sessions", {"name": "wrong"})[0] == 403
    assert not store.session_requests("team", "alpha", 1)


@pytest.mark.parametrize("value", ["true", 1, None, [], {}])
def test_control_flag_is_a_strict_boolean(web_env, value):
    _, auth, transport, settings, _ = web_env
    settings["web"]["control"] = value
    with pytest.raises(ValueError):
        HubWeb(settings, auth, transport=transport)
