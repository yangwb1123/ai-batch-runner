"""Real HTTP contracts for asynchronous session creation and user authority."""

from dataclasses import replace

import pytest

from pbatch.hub_auth import HubPrincipal, USER_SCOPES
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import HubService, PREFIX
from test_hub_http import hub  # noqa: F401


def register(call, instance="alpha", boot="one", capacity=2):
    status, value, _ = call("/gateway/register", method="POST", token=instance, body={
        "instance_id": instance, "boot_id": boot, "project_id": "operator-project",
        "session_capacity": capacity})
    assert status == 200, value
    assert value["data"]["session_capacity"] == capacity
    return value["data"]["generation"]


def create(call, instance="alpha", key="key", name="Review", token="alice"):
    return call(f"/instances/{instance}/sessions", method="POST", token=token,
                body={"name": name}, headers={"Idempotency-Key": key})


def report(call, request_id, *, generation=1, status="created", error_code="", token="alpha"):
    return call("/gateway/session-requests", method="POST", token=token, body={
        "instance_id": "alpha", "generation": generation, "request_id": request_id,
        "status": status, "error_code": error_code})


def test_http_creates_empty_instance_session_and_prompt_uses_existing_turn_route(hub):
    call, store, _ = hub
    register(call)
    assert call("/instances")[1]["data"][0]["session_capacity"] == 2
    status, value, headers = create(call, name=" Review ")
    assert status == 202 and headers["Cache-Control"] == "no-store"
    request = value["data"]
    assert create(call)[1]["data"] == request
    assert call("/sessions")[1]["data"] == []
    query = "/gateway/session-requests?instance_id=alpha&generation=1"
    status, value, _ = call(query, token="alpha")
    assert status == 200 and value["data"][0]["request_id"] == request["request_id"]
    assert value["data"][0]["project_id"] == "operator-project"
    status, value, _ = report(call, request["request_id"])
    assert status == 200 and value["data"]["status"] == "created"
    created = value["data"]
    assert report(call, request["request_id"])[1]["data"] == created
    assert call("/session-requests/" + request["request_id"])[1]["data"] == created
    assert call(query, token="alpha")[1]["data"] == []
    session = store.session("team", created["session_id"])
    assert session["controllable"] and session["name"] == "Review"
    status, _, _ = call(f"/sessions/{session['session_id']}/turns", method="POST",
                        body={"prompt": "Run review"}, headers={"Idempotency-Key": "turn"})
    assert status == 202


def test_http_session_requests_enforce_kind_scope_tenant_and_instance(hub):
    call, _store, _ = hub
    register(call)
    register(call, "beta")
    request_id = create(call, instance="beta")[1]["data"]["request_id"]
    assert create(call, token="alpha")[0] == 403
    assert create(call, token="alpha-reader")[0] == 403
    assert create(call, token="outsider")[0] == 404
    path = "/session-requests/" + request_id
    assert call(path, token="alpha-reader")[0] == 403
    assert call(path, token="outsider")[0] == 404
    assert call(path, token="beta")[0] == 403
    query = "/gateway/session-requests?instance_id=alpha&generation=1"
    assert call(query)[0] == 403
    assert call(query, token="beta")[0] == 403
    assert report(call, request_id, token="beta")[0] == 403
    assert report(call, request_id)[0] == 404
    assert call(path)[1]["data"]["status"] == "queued"


def test_http_takeover_preserves_lost_request_and_rejects_late_created_ack(hub):
    call, _store, clock = hub
    register(call)
    request_id = create(call)[1]["data"]["request_id"]
    clock[0] += 61
    assert create(call, key="offline")[1]["error"]["code"] == "instance_offline"
    assert create(call)[0] == 202
    assert register(call, boot="two") == 2
    lost = call("/session-requests/" + request_id)[1]["data"]
    assert lost["status"] == "lost" and lost["error_code"] == "owner_replaced"
    assert create(call)[1]["data"] == lost
    assert report(call, request_id)[1]["error"]["code"] == "stale_generation"
    assert report(call, request_id, generation=2)[1]["error"]["code"] == "stale_generation"
    assert call("/sessions")[1]["data"] == []


@pytest.mark.parametrize("field,value", [("cwd", "/private"), ("provider", "other"),
                                        ("model", "other"), ("tenant", "other"),
                                        ("project_id", "other"), ("local_session_id", "override")])
def test_create_rejects_operator_configuration_overrides(hub, field, value):
    call, store, _ = hub
    register(call)
    status, result, _ = call("/instances/alpha/sessions", method="POST", body={
        "name": "Review", field: value}, headers={"Idempotency-Key": "key"})
    assert status == 400 and result["error"]["code"] == "invalid_request"
    assert store.session_requests("team", "alpha", 1) == []


@pytest.mark.parametrize("field", ["local_session_id", "session_id", "project_id", "name"])
def test_ack_rejects_any_session_identity_or_configuration_override(hub, field):
    call, store, _ = hub
    register(call)
    request_id = create(call)[1]["data"]["request_id"]
    status, _, _ = call("/gateway/session-requests", method="POST", token="alpha", body={
        "instance_id": "alpha", "generation": 1, "request_id": request_id,
        "status": "created", field: "override"})
    assert status == 400 and store.sessions("team") == []


def test_new_routes_reject_missing_key_extra_query_and_get_body(hub):
    call, store, _ = hub
    register(call)
    assert create(call, key="")[0] == 400
    assert call("/instances/alpha/sessions?extra=x", method="POST", body={"name": "Review"},
                headers={"Idempotency-Key": "key"})[0] == 400
    request_id = create(call)[1]["data"]["request_id"]
    path = "/session-requests/" + request_id
    assert call(path + "?limit=1")[0] == 400
    assert call(path, body={"unexpected": "body"})[0] == 400
    query = "/gateway/session-requests?instance_id=alpha&generation=1"
    assert call(query + "&limit=1", token="alpha")[0] == 400
    assert call(query, token="alpha", body={"unexpected": "body"})[0] == 400
    assert call("/gateway/session-requests?extra=x", method="POST", token="alpha", body={
        "instance_id": "alpha", "generation": 1, "request_id": request_id,
        "status": "created"})[0] == 400
    assert store.sessions("team") == []


def test_request_status_checks_read_scope_before_lookup_and_membership_gate(hub, monkeypatch):
    _call, store, _ = hub
    service = HubService(store)
    principal = HubPrincipal("team", "alice", "cli", "user", frozenset({"alpha"}), USER_SCOPES)

    def unexpected_lookup(*_):
        pytest.fail("request lookup ran before scope authorization")

    monkeypatch.setattr(store, "session_request", unexpected_lookup)
    with pytest.raises(HubAccessError) as error:
        service.dispatch("GET", PREFIX + "/session-requests/opaque", {}, {},
                         replace(principal, scopes=frozenset()))
    assert error.value.code == "insufficient_scope"

    class Deny:
        def check(self, principal):
            raise HubAccessError("identity_ineligible", "membership denied")

    service.identity = Deny()
    with pytest.raises(HubAccessError) as error:
        service.dispatch("POST", PREFIX + "/instances/alpha/sessions", {},
                         {"name": "Review"}, principal, "key")
    assert error.value.code == "identity_ineligible"


def test_public_requests_omit_actor_client_key_project_and_local_identity(hub):
    call, _store, _ = hub
    register(call)
    request = create(call)[1]["data"]
    expected = {"request_id", "instance_id", "name", "status", "session_id", "error_code",
                "created_at", "updated_at"}
    assert set(request) == expected
    failed = report(call, request["request_id"], status="failed",
                    error_code="session_creation_failed")[1]["data"]
    assert set(failed) == expected and failed["session_id"] == ""
    assert report(call, request["request_id"])[1]["error"]["code"] == "session_request_terminal"
    assert create(call)[1]["data"] == failed
