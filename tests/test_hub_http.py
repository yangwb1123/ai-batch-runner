"""HTTP/SQLite contract tests for cross-instance sessions and commands."""

from __future__ import annotations

import hashlib
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from pbatch.hub_auth import GATEWAY_SCOPE, USER_SCOPES, HubAuth
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_service import PREFIX
from pbatch.hub_store import HubStore


def _grant(token, *, tenant="team", kind="user", instances=("alpha", "beta"), scopes=None):
    return {"subject": token, "client_id": token, "tenant_id": tenant, "kind": kind,
            "instances": list(instances), "scopes": list(scopes or USER_SCOPES),
            "token_sha256": hashlib.sha256(token.encode()).hexdigest()}


@pytest.fixture
def hub(tmp_path):
    clock = [1000.0]
    store = HubStore(tmp_path / "hub.sqlite3", clock=lambda: clock[0])
    grants = [_grant("alice"), _grant("alpha-reader", instances=("alpha",),
                                     scopes=["agent.instances:read", "agent.sessions:read"]),
              _grant("outsider", tenant="other")]
    for name in ("alpha", "beta"):
        grants.append(_grant(name, kind="gateway", instances=(name,), scopes=[GATEWAY_SCOPE]))
    auth = HubAuth({"mode": "development", "grants": grants})
    server = HubHTTPServer(("127.0.0.1", 0), store, auth,
                           allowed_origins=["https://console.test"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path, *, method="GET", body=None, token="alice", headers=None, raw=False):
        request_headers = {"Authorization": "Bearer " + token}
        request_headers.update(headers or {})
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(base + PREFIX + path, data=data, headers=request_headers, method=method)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            content = response.read().decode()
            return response.status, content if raw else json.loads(content), dict(response.headers)

    yield call, store, clock
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    store.close()


def publish(call, instance="alpha", local="local-session"):
    status, value, _ = call("/gateway/register", method="POST", token=instance,
                            body={"instance_id": instance, "boot_id": "boot-" + instance,
                                  "name": instance, "project_id": "project"})
    assert status == 200, value
    generation = value["data"]["generation"]
    status, value, _ = call("/gateway/sessions", method="POST", token=instance,
                            body={"instance_id": instance, "generation": generation,
                                  "local_session_id": local, "name": "Work", "project_id": "project",
                                  "controllable": True, "status": "idle"})
    assert status == 200, value
    return value["data"]["session_id"], generation


def send(call, session, prompt="Review the local project", key="client-request-1", token="alice"):
    return call(f"/sessions/{session}/turns", method="POST", token=token,
                body={"prompt": prompt}, headers={"Idempotency-Key": key})


def event(call, session, turn, generation, *, kind, event_id, payload=None, instance="alpha"):
    return call("/gateway/events", method="POST", token=instance, body={
        "instance_id": instance, "generation": generation, "session_id": session,
        "turn_id": turn, "kind": kind, "event_id": event_id, "payload": payload or {}})


def test_full_prompt_delivery_and_cursor_replay(hub):
    call, store, _clock = hub
    session, generation = publish(call)
    status, result, _ = send(call, session)
    assert status == 202, result
    turn = result["data"]["turn_id"]
    assert "prompt" not in result["data"]
    assert send(call, session)[1]["data"]["turn_id"] == turn
    assert send(call, session, prompt="different")[0] == 409
    status, commands, _ = call(f"/gateway/commands?instance_id=alpha&generation={generation}", token="alpha")
    assert status == 200
    assert commands["data"][0]["turn_id"] == turn
    assert commands["data"][0]["prompt"] == "Review the local project"
    assert event(call, session, turn, generation, kind="turn.running", event_id="e1")[0] == 200
    status, message, _ = event(call, session, turn, generation, kind="message.completed", event_id="e2",
                                payload={"text": "Analysis complete"})
    assert status == 200, message
    cursor = message["data"]["cursor"]
    assert event(call, session, turn, generation, kind="turn.completed", event_id="e3")[0] == 200
    assert event(call, session, turn, generation, kind="turn.completed", event_id="e3")[0] == 200
    status, page, _ = call(f"/sessions/{session}/events?after={cursor}")
    assert status == 200
    assert [row["event_id"] for row in page["data"]] == ["e3"]
    assert call(f"/turns/{turn}")[1]["data"]["state"] == "completed"
    assert store.events("team", session, after=cursor)[0]["event_id"] == "e3"


def test_same_local_session_id_on_two_instances_stays_separate(hub):
    call, _store, _clock = hub
    alpha, _ = publish(call, "alpha")
    beta, _ = publish(call, "beta")
    assert alpha != beta
    rows = call("/sessions")[1]["data"]
    assert {item["session_id"] for item in rows} == {alpha, beta}
    assert {item["instance_id"] for item in call("/sessions?instance_id=alpha")[1]["data"]} == {"alpha"}
    assert {item["session_id"] for item in call("/sessions", token="alpha-reader")[1]["data"]} == {alpha}
    assert call(f"/sessions/{beta}", token="alpha-reader")[0] == 403
    assert send(call, alpha, token="alpha-reader")[0] == 403
    assert call(f"/sessions/{alpha}", token="outsider")[0] == 404
    assert call("/sessions", token="outsider")[1]["data"] == []


def test_gateway_cannot_publish_or_execute_for_another_instance(hub):
    call, _store, _clock = hub
    alpha, generation = publish(call)
    beta, _ = publish(call, "beta")
    turn = send(call, beta)[1]["data"]["turn_id"]
    assert call("/gateway/register", method="POST", token="alpha",
                body={"instance_id": "beta", "boot_id": "steal"})[0] == 403
    assert event(call, beta, turn, generation, kind="turn.running", event_id="steal")[0] in {403, 404, 409}
    assert call("/sessions", token="alpha")[0] == 403
    assert call(f"/gateway/commands?instance_id=alpha&generation={generation}")[0] == 403
    assert send(call, alpha, key="fine")[0] == 202


def test_cancel_queued_turn_never_reaches_agent(hub):
    call, _store, _clock = hub
    session, generation = publish(call)
    turn = send(call, session)[1]["data"]["turn_id"]
    status, result, _ = call(f"/turns/{turn}/cancel", method="POST", body={})
    assert status == 202 and result["data"]["state"] == "cancelled"
    commands = call(f"/gateway/commands?instance_id=alpha&generation={generation}", token="alpha")[1]
    assert not commands["data"]
    assert send(call, session, key="next")[0] == 202


def test_live_cancel_has_distinct_request_and_stopped_states(hub):
    call, _store, _clock = hub
    session, generation = publish(call)
    turn = send(call, session)[1]["data"]["turn_id"]
    event(call, session, turn, generation, kind="turn.running", event_id="running")
    status, result, _ = call(f"/turns/{turn}/cancel", method="POST", body={})
    assert status == 202 and result["data"]["state"] == "cancel_requested"
    commands = call(f"/gateway/commands?instance_id=alpha&generation={generation}", token="alpha")[1]
    assert commands["data"][0]["cancel_requested"] is True
    status, retry, _ = event(call, session, turn, generation, kind="turn.running", event_id="running")
    assert status == 200 and retry["data"]["turn_state"] == "cancel_requested"
    assert retry["data"]["duplicate"] is True
    event(call, session, turn, generation, kind="turn.interrupted", event_id="stopped")
    assert call(f"/turns/{turn}")[1]["data"]["state"] == "interrupted"


def test_stale_gateway_cannot_write_after_takeover(hub):
    call, _store, clock = hub
    session, generation = publish(call)
    turn = send(call, session)[1]["data"]["turn_id"]
    assert call("/gateway/register", method="POST", token="alpha",
                body={"instance_id": "alpha", "boot_id": "new-boot"})[0] == 409
    clock[0] += 61
    assert call(f"/sessions/{session}")[1]["data"]["controllable"] is False
    assert send(call, session, key="stale")[0] == 409
    status, registered, _ = call("/gateway/register", method="POST", token="alpha",
                                 body={"instance_id": "alpha", "boot_id": "new-boot"})
    assert status == 200 and registered["data"]["generation"] > generation
    assert event(call, session, turn, generation, kind="turn.running", event_id="late")[0] == 409
    assert call(f"/turns/{turn}")[1]["data"]["state"] == "lost"


def test_auth_transport_input_bounds_and_sse(hub):
    call, _store, _clock = hub
    session, generation = publish(call)
    status, _, headers = call("/instances", token="invalid")
    assert status == 401 and "WWW-Authenticate" in headers
    assert call("/instances?token=leak")[0] == 400
    assert call("/instances?limit=0")[0] == 400
    assert call("/instances?limit=1&limit=2")[0] == 400
    assert call("/instances", headers={"Origin": "https://evil.test"})[0] == 403
    status, _, headers = call("/instances", headers={"Origin": "https://console.test"})
    assert status == 200 and headers["Access-Control-Allow-Origin"] == "https://console.test"
    assert headers["Cache-Control"] == "no-store"
    assert send(call, session, prompt="x" * 70000)[0] == 413
    assert send(call, session, prompt="x" * 16385)[0] == 400
    assert call(f"/sessions/{session}/turns", method="POST", body={"prompt": "hi", "cwd": "/"})[0] == 400
    turn = send(call, session)[1]["data"]["turn_id"]
    event(call, session, turn, generation, kind="turn.running", event_id="stream-start")
    status, stream, headers = call(f"/sessions/{session}/events", raw=True,
                                   headers={"Accept": "text/event-stream", "Last-Event-ID": "0"})
    assert status == 200 and "event: agent.event" in stream
    assert headers["Content-Type"].startswith("text/event-stream")


def test_development_listener_cannot_be_exposed_remotely(tmp_path):
    auth = HubAuth({"mode": "development", "grants": [_grant("alice")]})
    store = HubStore(tmp_path / "private.sqlite3")
    try:
        with pytest.raises(ValueError, match="loopback"):
            HubHTTPServer(("0.0.0.0", 0), store, auth)
    finally:
        store.close()
