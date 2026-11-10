"""Task discovery HTTP and Cookie facade retain exact resource authorization."""

import json
from urllib.parse import urlencode

import pytest

from test_hub_auth import signed
from test_hub_http import hub, publish, send, event
from test_hub_web import web_env
from test_hub_web_control import control, authenticated, create_session
from test_hub_web_read_grants import readers


def test_bearer_task_discovery_is_resource_scoped_and_metadata_only(hub):
    call, _, _ = hub
    first, generation = publish(call)
    publish(call, instance="beta")
    latest = send(call, first, prompt="token=private task detail")[1]["data"]["turn_id"]
    assert event(call, first, latest, generation, kind="turn.running", event_id="running")[0] == 200
    assert event(call, first, latest, generation, kind="turn.failed", event_id="failed")[0] == 200
    status, result, _ = call("/task-sessions?state=attention", token="alpha-reader")
    assert status == 200 and len(result["data"]) == 1
    row = result["data"][0]
    assert row["session_id"] == first and row["category"] == "attention"
    assert row["latest_turn"]["turn_id"] == latest and row["status"] == "idle"
    assert set(row["latest_turn"]) == {"turn_id", "state", "created_at", "updated_at"}
    assert "private" not in json.dumps(result) and "prompt" not in json.dumps(result)
    assert call("/task-sessions?instance_id=beta", token="alpha-reader")[0] == 403
    assert call("/task-sessions", token="alpha")[0] == 403
    assert call("/task-sessions", token="outsider")[1]["data"] == []
    assert call("/task-sessions", method="POST", body={})[0] == 405
    assert call("/task-sessions", body={})[0] == 400


def test_bearer_cursor_cannot_cross_principals_or_filter(hub):
    call, _, _ = hub
    for local in ("one", "two", "three"):
        publish(call, local=local)
    first = call("/task-sessions?limit=1")[1]
    cursor = first["next_cursor"]
    assert cursor
    suffix = urlencode({"after": cursor})
    assert len(call("/task-sessions?" + suffix)[1]["data"]) == 2
    assert call("/task-sessions?" + suffix, token="alpha-reader")[0] == 403
    assert call("/task-sessions?state=idle&" + suffix)[0] == 400


@pytest.mark.parametrize("suffix", [
    "?limit=0", "?limit=51", "?limit=true", "?limit=1&limit=2", "?state=failed", "?after=bad",
    "?before=0", "?unknown=1", "?q=" + "x" * 129, "?q=%00", "?q=%ff",
])
def test_task_http_rejects_invalid_queries(hub, suffix):
    call, *_ = hub
    assert call("/task-sessions" + suffix)[0] == 400


def test_browser_task_discovery_requires_exact_task_access(control, signed):
    call, server, store, *_ = control
    assert call("/web/api/task-sessions")[0] == 401
    cookie, access = authenticated(call)
    session = create_session(control, cookie, access)
    turn = store.submit_turn("team", session, "synthetic-user", "private prompt", "task-list-turn")
    store.cancel_turn("team", turn["turn_id"], "synthetic-user")
    status, _, raw = call("/web/api/task-sessions?state=attention&q=task", cookie=cookie)
    data = json.loads(raw)["data"]
    assert status == 200 and len(data) == 1
    assert data[0]["session_id"] == session and data[0]["latest_turn"]["state"] == "cancelled"
    assert data[0]["category"] == "attention" and "private prompt" not in raw.decode()
    for suffix in ("?limit=51", "?state=bad", "?before=bad", "?after=bad", "?q=%00"):
        assert call("/web/api/task-sessions" + suffix, cookie=cookie)[0] == 400
    assert call("/web/api/task-sessions?limit=50", cookie=cookie)[0] == 200
    assert call("/web/api/task-sessions?instance_id=beta", cookie=cookie)[0] == 403
    server.auth = server.web.auth = readers(signed[2])
    assert call("/web/api/sessions", cookie=cookie)[0] == 200
    assert call("/web/api/task-sessions", cookie=cookie)[0] == 403


def test_browser_task_listing_is_get_only_and_read_only_web_stays_disabled(control):
    call, server, *_ = control
    cookie, _ = authenticated(call)
    assert call("/web/api/task-sessions", cookie=cookie, method="POST", headers={"Origin": "https://abr.test"})[0] == 405
    assert call("/web/api/task-sessions", cookie=cookie, body=b"{}")[0] == 400
    server.web.control = False
    assert call("/web/api/task-sessions", cookie=cookie)[0] == 403


def test_browser_cursor_rejects_partial_instance_revocation_even_with_same_scopes(control):
    from dataclasses import replace
    call, server, store, generations, _ = control
    server.auth.grants[0] = replace(server.auth.grants[0], instances=frozenset({"alpha", "beta"}))
    cookie, access = authenticated(call)
    for index in range(2):
        store.upsert_session("team", "alpha", generations["alpha"], {
            "local_session_id": f"page-{index}", "name": "work", "project_id": "project",
            "controllable": True, "status": "idle"})
    status, _, raw = call("/web/api/task-sessions?limit=1", cookie=cookie)
    cursor = json.loads(raw)["next_cursor"]
    assert status == 200 and cursor
    server.auth.grants[0] = replace(server.auth.grants[0], instances=frozenset({"alpha"}))
    current = json.loads(call("/web/api/access", cookie=cookie)[2])["data"]
    assert current["scopes"] == access["scopes"] and current["context"] == access["context"]
    status, _, raw = call("/web/api/task-sessions?" + urlencode({"after": cursor}), cookie=cookie)
    assert status == 403 and json.loads(raw)["error"]["code"] == "task_cursor_authority_changed"
    assert call("/web/api/task-sessions", cookie=cookie)[0] == 200


@pytest.mark.parametrize("headers", [{"Idempotency-Key": "unused"}, {"If-Match": "revision"},
                                      {"Content-Length": "1"}])
def test_task_listing_rejects_mutation_and_body_headers(hub, headers):
    call, *_ = hub
    assert call("/task-sessions", headers=headers)[0] == 400


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "HEAD"])
def test_task_listing_unsupported_methods_use_authenticated_json_errors(hub, method):
    call, *_ = hub
    status, _, headers = call("/task-sessions", method=method, raw=True)
    assert status == 405
    assert headers["Content-Type"].startswith("application/json")
    assert call("/task-sessions", method=method, token="unknown", raw=True)[0] == 401


@pytest.mark.parametrize("headers", [{"Idempotency-Key": "unused"}, {"If-Match": "revision"}])
def test_browser_task_listing_rejects_mutation_headers(control, headers):
    call, *_ = control
    cookie, _ = authenticated(call)
    assert call("/web/api/task-sessions", cookie=cookie, headers=headers)[0] == 400
