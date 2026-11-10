"""Real HTTP/SQLite history search; synthetic identities, no external issuer/agent."""

import json
from dataclasses import replace
from urllib.parse import urlencode

from test_hub_auth import signed
from test_hub_http import hub, publish
from test_hub_web import web_env
from test_hub_web_control import control, authenticated, create_session
from test_hub_web_read_grants import readers


def recorded(store, sid, prompt, key):
    turn = store.submit_turn("team", sid, "synthetic-user", prompt, key)
    store.cancel_turn("team", turn["turn_id"], "synthetic-user")
    return turn["turn_id"]


def test_bearer_search_and_exact_lookup_reuse_resource_authorization(hub):
    call, store, _ = hub
    sid, _ = publish(call)
    target = recorded(store, sid, "password=private-oracle Straße", "selected")
    path = f"/sessions/{sid}/turns"
    status, result, _ = call(path + "?" + urlencode({"q": "STRASSE", "state": "cancelled"}), token="alpha-reader")
    assert status == 200 and result["data"][0]["turn_id"] == target
    assert result["data"][0]["prompt"] == "password=[REDACTED] Straße"
    assert call(path + "?q=private-oracle", token="alpha-reader")[1] == {"data": [], "next_cursor": ""}
    assert call(path + "?turn_id=" + target, token="alpha-reader")[1] == result
    for token in ("outsider", "alpha"):
        assert call(path + "?turn_id=" + target, token=token)[0] in {403, 404}
    other, _ = publish(call, instance="beta")
    assert call(f"/sessions/{other}/turns?turn_id={target}", token="alpha-reader")[0] == 403
    assert call(f"/sessions/{other}/turns?turn_id={target}")[1] == {"data": [], "next_cursor": ""}
    assert store.commands("team", "alpha", 1) == []


def test_bearer_query_contract_rejects_duplicates_bad_filters_and_get_bodies(hub):
    call, _, _ = hub
    sid, _ = publish(call)
    path = f"/sessions/{sid}/turns"
    for query in ("q=a&q=b", "state=unknown", "turn_id=", "turn_id=t&q=search", "turn_id=t&state=failed",
                  "turn_id=t&before=x", "q=%00", "q=%C2%85", "q=%E2%80%AE", "q=%FF", "q=" + "x" * 129):
        assert call(path + "?" + query)[0] == 400
    assert call(path + "?q=search", body={})[0] == 400
    assert call(path + "?q=search", headers={"Content-Length": "1"})[0] == 400


def test_browser_search_exact_read_and_revoke_preserve_task_grants(control, signed):
    call, server, store, *_ = control
    cookie, access = authenticated(call)
    sid = create_session(control, cookie, access)
    target = recorded(store, sid, "token=secret-oracle inspect", "selected")
    path = f"/web/api/sessions/{sid}/turns"
    status, _, raw = call(path + "?q=inspect&state=cancelled", cookie=cookie)
    result = json.loads(raw)
    assert status == 200 and [row["turn_id"] for row in result["data"]] == [target]
    assert result["data"][0]["prompt"] == "token=[REDACTED] inspect"
    assert json.loads(call(path + "?q=secret-oracle", cookie=cookie)[2])["data"] == []
    status, _, raw = call(path + "?turn_id=" + target + "&limit=1", cookie=cookie)
    assert status == 200 and json.loads(raw) == result
    server.auth = server.web.auth = readers(signed[2])
    for query in ("q=inspect", "turn_id=" + target):
        assert call(path + "?" + query, cookie=cookie)[0] == 403


def test_browser_query_body_and_response_bounds_remain_enforced(control):
    call, _, store, *_ = control
    cookie, access = authenticated(call)
    sid = create_session(control, cookie, access)
    path = f"/web/api/sessions/{sid}/turns"
    for index in range(21):
        recorded(store, sid, "visible" + "\x00" * (16384 - 7), f"max-{index}")
    status, _, raw = call(path + "?q=visible&limit=20", cookie=cookie)
    assert status == 200 and len(raw) < 2 * 1024 * 1024
    result = json.loads(raw)
    assert len(result["data"]) == 20 and result["next_cursor"]
    for query in ("limit=21", "q=a&q=b", "q=%FF", "turn_id=", "q=" + "x" * 129,
                  "q=%C2%85", "q=%E2%81%A6", "turn_id=t&state=completed"):
        assert call(path + "?" + query, cookie=cookie)[0] == 400
    assert call(path + "?q=visible", cookie=cookie, body=b"{}")[0] == 400
    assert call(path + "?q=visible", cookie=cookie, headers={"Transfer-Encoding": "chunked"})[0] == 400


def test_browser_cursor_rejects_changed_authority_and_filters(control):
    call, server, store, *_ = control
    cookie, access = authenticated(call)
    sid = create_session(control, cookie, access)
    for index in range(3):
        recorded(store, sid, "visible", f"row-{index}")
    path = f"/web/api/sessions/{sid}/turns"
    first = json.loads(call(path + "?q=visible&limit=1", cookie=cookie)[2])
    cursor = first["next_cursor"]
    assert call(path + "?" + urlencode({"q": "changed", "before": cursor}), cookie=cookie)[0] == 400
    server.auth.grants[0] = replace(server.auth.grants[0], instances=frozenset({"alpha", "beta"}))
    status, _, raw = call(path + "?" + urlencode({"q": "visible", "before": cursor}), cookie=cookie)
    assert status == 403 and json.loads(raw)["error"]["code"] == "turn_cursor_authority_changed"


def test_http_budget_failure_is_a_503_error_never_an_empty_search_result(hub, monkeypatch):
    from pbatch import hub_turn_query
    call, store, _ = hub
    sid, _ = publish(call)
    for index in range(3):
        recorded(store, sid, "ordinary", f"row-{index}")
    monkeypatch.setattr(hub_turn_query, "QUERY_SCAN_ROWS", 1)
    status, result, _ = call(f"/sessions/{sid}/turns?q=absent")
    assert status == 503 and result["error"]["code"] == "turn_query_budget"
    assert "data" not in result and "next_cursor" not in result


def test_strict_history_query_decoding_does_not_expand_other_route_contracts():
    from pbatch.hub_http import _strict_query_path
    from pbatch.hub_service import PREFIX
    for suffix in ("/sessions/sid/turns", "/sessions/sid/%74urns"):
        assert _strict_query_path(PREFIX + suffix, "GET") is True
        assert _strict_query_path(PREFIX + suffix, "POST") is False
    for suffix in ("/sessions/sid", "/turns/tid", "/sessions/sid/events", "/sessions/turns"):
        assert _strict_query_path(PREFIX + suffix) is False
