"""Discover durable requests through real HTTP; only provider execution is substituted."""

import contextlib
import json
import threading

import pytest

from pbatch import hub_cli
from pbatch.hub_auth import GATEWAY_SCOPE, USER_SCOPES, HubAuth
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore
from test_hub_integration import _grant, _until
from test_hub_session_creation_integration import fleet  # noqa: F401


@contextlib.contextmanager
def serve(store, grants):
    server = HubHTTPServer(("127.0.0.1", 0), store,
                           HubAuth({"mode": "development", "grants": grants}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class Reader(HubClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.methods = []

    def _request(self, method, *args, **kwargs):
        self.methods.append(method)
        return super()._request(method, *args, **kwargs)


class LostAcknowledgement(HubClient):
    def _request(self, method, path, *args, **kwargs):
        result = super()._request(method, path, *args, **kwargs)
        if method == "POST":
            raise HubError("PRIVATE lost response", code="hub_unavailable")
        return result


def create_ready(user, gateways, instance, name, key):
    request = user.create_session(instance, name, idempotency_key=key)
    _until(gateways, lambda: user.session_request(request["request_id"])["status"] == "created")
    return user.session_request(request["request_id"])


def test_new_client_finds_an_accepted_creation_without_replaying_its_lost_ack(fleet):
    user, create, gateways, runtimes, _store, _clock = fleet
    create("alpha")
    interrupted = LostAcknowledgement(user.base_url, "user")
    with pytest.raises(HubError):
        interrupted.create_session("alpha", "Resume analysis", idempotency_key="private-create-key")
    reader = Reader(user.base_url, "user")
    row = reader.list_session_operations("alpha")["data"][0]
    assert row["operation"] == "create" and row["status"] == "queued"
    assert "private-create-key" not in json.dumps(row)
    _until(gateways, lambda: reader.session_request(row["request_id"])["status"] == "created")
    found = reader.session_request(row["request_id"])
    assert reader.list_session_operations("alpha")["data"][0]["session_id"] == found["session_id"]
    session = reader.get_session(found["session_id"])
    assert session["instance_id"] == "alpha" and session["controllable"]
    assert len(runtimes["alpha"]) == 1 and set(reader.methods) == {"GET"}
    turn = user.send_prompt(session["session_id"], "explicit new work", "new-turn")
    _until(gateways, lambda: user.get_turn(turn["turn_id"])["state"] == "completed")
    assert runtimes["alpha"][0].prompts == ["explicit new work"]


def test_new_client_finds_close_result_and_keeps_reading_offline_session_history(fleet):
    user, create, gateways, _runtimes, _store, clock = fleet
    gateway = create("alpha")
    created = create_ready(user, gateways, "alpha", "Preserved", "first")
    session_id = created["session_id"]
    turn = user.send_prompt(session_id, "preserve result", "history")
    _until(gateways, lambda: user.get_turn(turn["turn_id"])["state"] == "completed")
    history = user.session_events(session_id)
    interrupted = LostAcknowledgement(user.base_url, "user")
    with pytest.raises(HubError):
        interrupted.close_session(session_id, idempotency_key="private-close-key")
    reader = Reader(user.base_url, "user")
    row = next(row for row in reader.list_session_operations("alpha")["data"]
               if row["operation"] == "close")
    _until(gateways, lambda: reader.session_close_request(row["request_id"])["status"] == "closed")
    gateway.close()
    clock[0] += 61
    found = next(row for row in reader.list_session_operations("alpha")["data"]
                 if row["operation"] == "close")
    assert found["status"] == "closed" and found["session_id"] == session_id
    assert found["name"] == "" and "private-close-key" not in json.dumps(found)
    session = reader.get_session(session_id)
    assert not session["online"] and not session["controllable"]
    assert reader.session_events(session_id) == history and set(reader.methods) == {"GET"}


def test_other_authorized_client_sees_shared_history_but_never_other_grants(fleet):
    user, create, gateways, _runtimes, store, _clock = fleet
    create("alpha")
    create("beta")
    first = create_ready(user, gateways, "alpha", "Shared alpha", "alpha")
    create_ready(user, gateways, "beta", "Private beta", "beta")
    grants = [_grant("reader", "user", ["alpha"], ["agent.sessions:read"]),
              _grant("machine", "gateway", ["alpha"], [GATEWAY_SCOPE]),
              dict(_grant("other", "user", ["alpha"], ["agent.sessions:read"]), tenant_id="other")]
    with serve(store, grants) as origin:
        reader = Reader(origin, "reader")
        page = reader.list_session_operations(limit=1)
        assert [row["request_id"] for row in page["data"]] == [first["request_id"]]
        assert page["next_cursor"] == ""
        assert reader.get_session(page["data"][0]["session_id"])["instance_id"] == "alpha"
        assert set(reader.methods) == {"GET"}
        for operation in (lambda: reader.list_session_operations("beta"),
                          lambda: reader.create_session("alpha", "Denied", idempotency_key="denied"),
                          lambda: HubClient(origin, "machine").list_session_operations()):
            with pytest.raises(HubError) as denied:
                operation()
            assert denied.value.status == 403
        assert HubClient(origin, "other").list_session_operations()["data"] == []


def test_history_survives_hub_restart_and_tracks_old_generation_without_replay(tmp_path):
    clock = [1000.0]
    path = tmp_path / "hub.sqlite3"
    grants = [_grant("user", "user", ["alpha"], USER_SCOPES),
              _grant("alpha", "gateway", ["alpha"], [GATEWAY_SCOPE])]
    store = HubStore(path, clock=lambda: clock[0])
    try:
        with serve(store, grants) as origin:
            machine, user = HubClient(origin, "alpha"), HubClient(origin, "user")
            machine.register_instance(instance_id="alpha", boot_id="old", name="Alpha",
                                      project_id="private-project", session_capacity=2)
            request = user.create_session("alpha", "Pending across restart", idempotency_key="pending")
            first = user.list_session_operations("alpha")
    finally:
        store.close()
    store = HubStore(path, clock=lambda: clock[0])
    try:
        with serve(store, grants) as origin:
            reader = Reader(origin, "user")
            assert reader.list_session_operations("alpha") == first
            clock[0] += 61
            HubClient(origin, "alpha").register_instance(
                instance_id="alpha", boot_id="replacement", name="Alpha",
                project_id="private-project", session_capacity=2)
            row = reader.list_session_operations("alpha")["data"][0]
            assert row["request_id"] == request["request_id"] and row["status"] == "lost"
            assert row["error_code"] == "owner_replaced" and row["session_id"] == ""
            assert reader.list_sessions("alpha") == [] and set(reader.methods) == {"GET"}
    finally:
        store.close()


def test_cli_pages_creation_and_close_records_with_only_explicit_gets(fleet, monkeypatch, capsys):
    user, create, gateways, runtimes, _store, clock = fleet
    create("alpha")
    first = create_ready(user, gateways, "alpha", "Original", "hidden-create")
    clock[0] += 1
    close = user.close_session(first["session_id"], idempotency_key="hidden-close")
    _until(gateways, lambda: user.session_close_request(close["request_id"])["status"] == "closed")
    reader = Reader(user.base_url, "user")
    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *_a, **_k: reader)
    args = ["session-operations", "--instance", "alpha", "--limit", "1", "--json"]
    assert hub_cli.hub_main(args) == 0
    output = capsys.readouterr()
    newest = json.loads(output.out)
    assert newest["data"][0]["operation"] == "close" and newest["next_cursor"]
    assert hub_cli.hub_main(args + ["--before", newest["next_cursor"]]) == 0
    older = json.loads(capsys.readouterr().out)
    assert older["data"][0]["request_id"] == first["request_id"]
    assert older["next_cursor"] == "" and reader.methods == ["GET", "GET"]
    assert len(runtimes["alpha"]) == 1
    assert all(value not in json.dumps([newest, older]) for value in ("hidden-create", "hidden-close"))
