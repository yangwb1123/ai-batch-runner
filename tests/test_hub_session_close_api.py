"""Real HTTP close lifecycle contracts and authorization ordering."""

from dataclasses import replace

import pytest

from pbatch.hub_auth import HubPrincipal, USER_SCOPES
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import HubService, PREFIX
from test_hub_http import hub  # noqa: F401


def publish(call, instance="alpha", supported=True):
    status, value, _ = call("/gateway/register", method="POST", token=instance, body={
        "instance_id": instance, "boot_id": "boot", "session_capacity": 1,
        "session_close_supported": supported})
    assert status == 200 and value["data"]["session_close_supported"] is supported
    status, value, _ = call("/gateway/sessions", method="POST", token=instance, body={
        "instance_id": instance, "generation": 1, "local_session_id": "local",
        "controllable": True})
    assert status == 200
    return value["data"]["session_id"]


def close(call, session, key="key", token="alice", **kwargs):
    return call(f"/sessions/{session}/close", method="POST", token=token,
                body={}, headers={"Idempotency-Key": key}, **kwargs)


def report(call, request_id, **overrides):
    return call("/gateway/session-close-requests", method="POST", token="alpha", body={
        "instance_id": "alpha", "generation": 1, "request_id": request_id,
        "status": "closed", "error_code": "", **overrides})


def test_http_close_freezes_then_success_releases_capacity_and_keeps_history(hub):
    call, store, _ = hub
    session = publish(call)
    assert call("/instances")[1]["data"][0]["session_close_supported"] is True
    status, value, headers = close(call, session)
    assert status == 202 and headers["Cache-Control"] == "no-store"
    request = value["data"]
    assert close(call, session)[1]["data"] == request
    assert call("/sessions/" + session)[1]["data"]["status"] == "closing"
    query = "/gateway/session-close-requests?instance_id=alpha&generation=1"
    assert call(query, token="alpha")[1]["data"] == [{
        "request_id": request["request_id"], "instance_id": "alpha", "generation": 1,
        "session_id": session, "local_session_id": "local"}]
    status, value, _ = report(call, request["request_id"])
    assert status == 200 and value["data"]["status"] == "closed"
    assert report(call, request["request_id"])[1] == value
    assert call("/session-close-requests/" + request["request_id"])[1] == value
    assert close(call, session)[1] == value
    assert call(query, token="alpha")[1]["data"] == []
    assert store.session("team", session)["frozen"]
    assert call("/sessions")[1]["data"][0]["session_id"] == session
    status, _, _ = call("/instances/alpha/sessions", method="POST", body={"name": "Next"},
                        headers={"Idempotency-Key": "new"})
    assert status == 202


def test_close_routes_enforce_kind_scope_tenant_and_instance(hub):
    call, _, _ = hub
    alpha, beta = publish(call), publish(call, "beta")
    assert close(call, alpha, token="alpha")[0] == 403
    assert close(call, alpha, token="alpha-reader")[0] == 403
    assert close(call, alpha, token="outsider")[0] == 404
    request = close(call, beta)[1]["data"]
    path = "/session-close-requests/" + request["request_id"]
    assert call(path, token="alpha-reader")[0] == 403
    assert call(path, token="outsider")[0] == 404
    assert call(path, token="beta")[0] == 403
    query = "/gateway/session-close-requests?instance_id=alpha&generation=1"
    assert call(query)[0] == 403
    assert call(query, token="beta")[0] == 403
    assert report(call, request["request_id"])[0] == 404
    assert report(call, request["request_id"], instance_id="beta")[0] == 403
    assert call(path)[1]["data"] == request


@pytest.mark.parametrize("field", ["tenant", "actor", "session_id", "local_session_id",
                                  "generation", "force", "reason"])
def test_close_rejects_identity_or_force_overrides(hub, field):
    call, store, _ = hub
    session = publish(call)
    status, _, _ = call(f"/sessions/{session}/close", method="POST", body={field: "private"},
                        headers={"Idempotency-Key": "key"})
    assert status == 400 and not store.session("team", session)["frozen"]


def test_new_routes_reject_queries_missing_key_get_bodies_and_wrong_methods(hub):
    call, _, _ = hub
    session = publish(call)
    path = f"/sessions/{session}/close"
    assert close(call, session, key="")[0] == 400
    assert call(path + "?force=1", method="POST", body={},
                headers={"Idempotency-Key": "key"})[0] == 400
    assert call(path)[0] == 405
    request_id = close(call, session)[1]["data"]["request_id"]
    path = "/session-close-requests/" + request_id
    assert call(path, method="POST", body={})[0] == 405
    assert call(path + "?extra=1")[0] == 400
    assert call(path, body={})[0] == 400
    query = "/gateway/session-close-requests?instance_id=alpha&generation=1"
    assert call(query, token="alpha", body={})[0] == 400
    assert call(query + "&extra=1", token="alpha")[0] == 400
    assert call("/gateway/session-close-requests?extra=1", method="POST", token="alpha", body={
        "instance_id": "alpha", "generation": 1, "request_id": request_id,
        "status": "closed"})[0] == 400


@pytest.mark.parametrize("field", ["session_id", "local_session_id", "tenant", "actor"])
def test_close_report_cannot_override_session_identity(hub, field):
    call, _, _ = hub
    request = close(call, publish(call))[1]["data"]
    assert report(call, request["request_id"], **{field: "other"})[0] == 400
    assert call("/session-close-requests/" + request["request_id"])[1]["data"] == request


def test_status_scope_and_membership_checked_before_store_lookup(hub, monkeypatch):
    _, store, _ = hub
    service = HubService(store)
    user = HubPrincipal("team", "alice", "cli", "user", frozenset({"alpha"}), USER_SCOPES)

    def unexpected(*_):
        pytest.fail("lookup ran before authorization")

    monkeypatch.setattr(store, "session_close_request", unexpected)
    monkeypatch.setattr(store, "session", unexpected)
    for path, method in (("/session-close-requests/opaque", "GET"), ("/sessions/opaque/close", "POST")):
        with pytest.raises(HubAccessError) as error:
            service.dispatch(method, PREFIX + path, {}, {}, replace(user, scopes=frozenset()), "key")
        assert error.value.code == "insufficient_scope"

    class Deny:
        def check(self, principal):
            raise HubAccessError("identity_ineligible", "membership denied")

    service.identity = Deny()
    with pytest.raises(HubAccessError) as error:
        service.dispatch("POST", PREFIX + "/sessions/opaque/close", {}, {}, user, "key")
    assert error.value.code == "identity_ineligible"


def test_http_unsupported_busy_and_terminal_errors_use_safe_codes(hub):
    call, store, _ = hub
    session = publish(call, supported=False)
    assert close(call, session)[1]["error"]["code"] == "session_close_unsupported"
    publish(call)
    turn = store.submit_turn("team", session, "alice", "private prompt", "turn")
    status, value, _ = close(call, session)
    assert status == 409 and value["error"]["code"] == "session_busy"
    assert "private prompt" not in str(value)
    store.cancel_turn("team", turn["turn_id"], "alice")
    request = close(call, session)[1]["data"]
    failed = report(call, request["request_id"], status="failed", error_code="session_close_failed")
    assert failed[0] == 200 and failed[1]["data"]["status"] == "failed"
    assert close(call, session, key="new")[1]["error"]["code"] == "session_close_failed"
    assert report(call, request["request_id"])[1]["error"]["code"] == "session_request_terminal"


def test_wrong_http_methods_return_405_without_mutating_close_requests(hub):
    call, store, _ = hub
    session = publish(call)
    request = close(call, session)[1]["data"]
    paths = [(f"/sessions/{session}/close", "alice"),
             ("/session-close-requests/" + request["request_id"], "alice"),
             ("/gateway/session-close-requests", "alpha")]
    for path, token in paths:
        for method in ("PUT", "PATCH", "DELETE", "HEAD"):
            status, _, _ = call(path, method=method, token=token, raw=True)
            assert status == 405, (path, method, status)
    assert store.session_close_request("team", request["request_id"]) == request


def test_close_rejects_nonobject_json_and_get_transfer_encoding(hub):
    call, store, _ = hub
    session = publish(call)
    for body in ([], "text", False, 0):
        assert call(f"/sessions/{session}/close", method="POST", body=body,
                    headers={"Idempotency-Key": "key"})[0] == 400
    request = close(call, session)[1]["data"]
    for path, token in (("/session-close-requests/" + request["request_id"], "alice"),
                        ("/gateway/session-close-requests?instance_id=alpha&generation=1", "alpha")):
        assert call(path, token=token, headers={"Transfer-Encoding": "chunked"})[0] == 400
    assert store.session_close_request("team", request["request_id"]) == request


@pytest.mark.parametrize("status", ["queued", "closed", "failed"])
def test_http_replays_lost_terminal_event_ack_after_close_without_fencing_instance(hub, status):
    call, store, _ = hub
    session = publish(call)
    turn = store.submit_turn("team", session, "alice", "Work", "turn")
    body = {"instance_id": "alpha", "generation": 1, "event_id": "done", "session_id": session,
            "turn_id": turn["turn_id"], "kind": "turn.completed", "payload": {}}
    assert call("/gateway/events", method="POST", token="alpha", body={
        **body, "event_id": "start", "kind": "turn.running"})[0] == 200
    original = call("/gateway/events", method="POST", token="alpha", body=body)[1]["data"]
    request = close(call, session)[1]["data"]
    if status != "queued":
        assert report(call, request["request_id"], status=status,
                      error_code="session_close_failed" if status == "failed" else "")[0] == 200
    before = call(f"/sessions/{session}/events")[1]
    result, value, _ = call("/gateway/events", method="POST", token="alpha", body=body)
    assert result == 200 and value["data"] == {**original, "duplicate": True}
    result, value, _ = call("/gateway/events", method="POST", token="alpha", body={
        **body, "event_id": "late-message", "kind": "message.completed"})
    assert result == 409 and value["error"]["code"] == "session_read_only"
    assert call(f"/sessions/{session}/events")[1] == before
