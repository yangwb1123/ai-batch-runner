"""The request center exposes only authorized, already accepted operations."""

from dataclasses import replace
from email.message import Message
from urllib.parse import quote

import pytest

from pbatch.hub_auth import HubPrincipal, USER_SCOPES
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import HubService, PREFIX
from pbatch.hub_session_request_api import validate_read_headers
from test_hub_http import hub  # noqa: F401
from test_hub_session_operations import seed


PATH = "/session-operations"


def test_http_history_reopens_accepted_create_and_close_without_replay(hub):
    call, store, _ = hub
    store.register_instance("team", "alpha", {
        "boot_id": "boot", "session_capacity": 1, "session_close_supported": True})
    status, value, _ = call("/instances/alpha/sessions", method="POST", body={"name": "Review"},
                             headers={"Idempotency-Key": "create-key"})
    assert status == 202
    request = value["data"]
    made = store.report_session_request("team", "alpha", 1, {
        "request_id": request["request_id"], "status": "created"})
    status, value, _ = call("/sessions/" + made["session_id"] + "/close", method="POST", body={},
                             headers={"Idempotency-Key": "close-key"})
    assert status == 202
    closed = value["data"]
    # A different authorized subject/client can rediscover both durable receipts.
    status, value, headers = call(PATH + "?instance_id=alpha", token="alpha-reader")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert set(value) == {"data", "next_cursor"} and value["next_cursor"] == ""
    assert [(row["operation"], row["request_id"]) for row in value["data"]] == [
        ("create", request["request_id"]), ("close", closed["request_id"])]
    assert value["data"][0]["session_id"] == value["data"][1]["session_id"] == made["session_id"]
    assert value["data"][1]["name"] == ""
    assert store.session_close_request("team", closed["request_id"])["status"] == "queued"
    assert len(store.sessions("team")) == 1
    for row in value["data"]:
        route = "/session-requests/" if row["operation"] == "create" else "/session-close-requests/"
        assert call(route + row["request_id"], token="alpha-reader")[0] == 200


def test_http_history_filters_tenant_instance_and_actor_client_before_paginating(hub):
    call, store, _ = hub
    seed(store, "close", "alpha-work", at=1000)
    for index in range(4):
        seed(store, "create", "beta-" + str(index), at=2000 + index, instance="beta")
    seed(store, "create", "other-tenant", at=3000, tenant="other")
    status, page, _ = call(PATH + "?limit=1", token="alpha-reader")
    assert status == 200 and [row["request_id"] for row in page["data"]] == ["alpha-work"]
    assert page["next_cursor"] == ""
    assert call(PATH + "?instance_id=beta", token="alpha-reader")[0] == 403
    assert call(PATH, token="alpha")[0] == 403
    assert call(PATH, token="outsider")[1]["data"][0]["request_id"] == "other-tenant"
    status, broad, _ = call(PATH + "?limit=1")
    assert status == 200 and broad["next_cursor"]
    # Even a cursor obtained from a broader grant cannot widen a narrower grant.
    restricted = call(PATH + "?before=" + broad["next_cursor"], token="alpha-reader")[1]
    assert [row["request_id"] for row in restricted["data"]] == ["alpha-work"]
    assert call(PATH, token="invalid")[0] == 401


def test_history_survives_offline_generation_takeover_and_empty_grants(hub):
    call, store, clock = hub
    store.register_instance("team", "alpha", {"boot_id": "one", "session_capacity": 1})
    request = store.create_session("team", "alpha", "alice", "Old work", "key", client_id="cli")
    clock[0] += 1000
    store.register_instance("team", "alpha", {"boot_id": "two", "session_capacity": 1})
    clock[0] += 1000
    page = call(PATH + "?instance_id=alpha")[1]
    assert page["data"][0]["request_id"] == request["request_id"]
    assert page["data"][0]["status"] == "lost" and page["data"][0]["error_code"] == "owner_replaced"
    user = HubPrincipal("team", "alice", "web", "user", frozenset(), USER_SCOPES)
    assert HubService(store).dispatch("GET", PREFIX + PATH, {}, {}, user) == (
        200, {"data": [], "next_cursor": ""})


def test_scope_and_identity_enforcement_precede_history_storage(hub, monkeypatch):
    _, store, _ = hub
    user = HubPrincipal("team", "alice", "web", "user", frozenset({"alpha"}), USER_SCOPES)

    def unexpected(*_args, **_kwargs):
        pytest.fail("read happened before authorization")

    monkeypatch.setattr(store, "session_operations", unexpected)
    service = HubService(store)
    for principal, query, code in (
            (replace(user, scopes=frozenset()), {}, "insufficient_scope"),
            (user, {"instance_id": "beta"}, "forbidden"),
            (replace(user, kind="gateway"), {}, "forbidden")):
        with pytest.raises(HubAccessError) as captured:
            service.dispatch("GET", PREFIX + PATH, query, {}, principal)
        assert captured.value.code == code

    class Deny:
        def check(self, _principal):
            raise HubAccessError("identity_ineligible", "membership denied")

    service.identity = Deny()
    with pytest.raises(HubAccessError) as captured:
        service.dispatch("GET", PREFIX + PATH, {}, {}, user)
    assert captured.value.code == "identity_ineligible"


@pytest.mark.parametrize("query", ["?limit=0", "?limit=51", "?limit=-1", "?limit=true", "?limit=1.5",
                                   "?limit=", "?limit=NaN", "?limit=Inf", "?limit=1&limit=2",
                                   "?before=%3D", "?before=" + "x" * 2049,
                                   "?instance_id=", "?instance_id=bad%00id", "?instance_id=bad%7Fid",
                                   "?operation=create", "?status=queued", "?after=old", "?tenant=other"])
def test_http_rejects_unsupported_or_unbounded_query(hub, query):
    call, _, _ = hub
    status, body, _ = call(PATH + query)
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_empty_pages_default_limit_and_c1_identifiers_follow_existing_contract(hub):
    call, store, _ = hub
    assert call(PATH)[1] == {"data": [], "next_cursor": ""}
    for index in range(25):
        seed(store, "create", f"work-{index:02}")
    assert len(call(PATH)[1]["data"]) == 20
    assert len(call(PATH + "?limit=50")[1]["data"]) == 25
    assert len(call(PATH + "?limit=00001")[1]["data"]) == 1
    instance = "节点\x80"
    seed(store, "close", "unicode\x80", instance=instance)
    principal = HubPrincipal("team", "reader", "mobile", "user", frozenset({instance}), USER_SCOPES)
    status, result = HubService(store).dispatch("GET", PREFIX + PATH, {"instance_id": instance}, {}, principal)
    assert status == 200 and result["data"][0]["request_id"] == "unicode\x80"
    assert call(PATH + "?instance_id=" + quote(instance), token="alpha-reader")[0] == 403


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_new_history_route_rejects_mutating_and_wrong_methods(hub, method):
    call, store, _ = hub
    seed(store, "create", "untouched")
    status, body, _ = call(PATH, method=method, body={} if method == "POST" else None, raw=method == "HEAD")
    assert status == 405
    if method != "HEAD":
        assert body["error"]["code"] == "method_not_allowed"
    assert len(store.session_operations("team")["data"]) == 1


def test_get_bodies_and_transport_headers_are_rejected_without_consuming_them(hub):
    call, _, _ = hub
    assert call(PATH, body={})[0] == 400
    for values in (("Content-Length", "1"), ("Transfer-Encoding", "chunked")):
        headers = Message()
        headers[values[0]] = values[1]
        with pytest.raises(ValueError):
            validate_read_headers(PREFIX + PATH, headers)
    headers = Message()
    headers["Content-Length"] = "0"
    headers["Content-Length"] = "0"
    with pytest.raises(ValueError):
        validate_read_headers(PREFIX + PATH, headers)
    validate_read_headers(PREFIX + PATH, Message())
    headers = Message()
    headers["Content-Length"] = "0"
    validate_read_headers(PREFIX + PATH, headers)


def test_no_idempotency_key_is_required_and_listing_never_creates_new_requests(hub):
    call, store, _ = hub
    seed(store, "create", "known")
    for _ in range(3):
        assert call(PATH)[0] == 200
    assert [row["request_id"] for row in store.session_operations("team")["data"]] == ["known"]
