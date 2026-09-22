"""Request-history pagination, bounded wire validation and read-only CLI."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from pbatch import hub_cli
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_session_operations_client import operation_record, page, refreshed_record


def record(**changes):
    return dict(dict(operation="create", request_id="create-1", instance_id="alpha", name="Review",
                     status="created", session_id="session-1", error_code="", created_at=2,
                     updated_at=3), **changes)


def close_record(**changes):
    return record(**dict(dict(operation="close", name="", request_id="close-1", status="closed"),
                         **changes))


@pytest.fixture
def endpoint():
    state = SimpleNamespace(body={"data": [record(), close_record()], "next_cursor": "cursor_1"},
                            status=200, calls=[], raw=None)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.calls.append((self.command, self.path, dict(self.headers)))
            body = state.raw if state.raw is not None else json.dumps(state.body).encode()
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield HubClient(f"http://127.0.0.1:{server.server_port}", "fake-token"), state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_one_bounded_page_uses_get_and_keeps_opaque_cursor(endpoint):
    client, state = endpoint
    value = client.list_session_operations("alpha", before="old-CURSOR", limit=2)
    assert value == state.body and len(state.calls) == 1
    method, path, headers = state.calls[0]
    assert method == "GET" and urlsplit(path).path == "/api/v1/agent/session-operations"
    assert parse_qs(urlsplit(path).query) == {"instance_id": ["alpha"], "before": ["old-CURSOR"],
                                           "limit": ["2"]}
    assert "Content-Length" not in headers and "Idempotency-Key" not in headers
    state.body = {"data": [], "next_cursor": ""}
    assert client.list_session_operations(before="cursor_1") == state.body
    assert "instance_id" not in parse_qs(urlsplit(state.calls[-1][1]).query)


@pytest.mark.parametrize("instance,before,limit", [(None, "", 20), (" bad", "", 20),
    ("a\n", "", 20), ("", "abc=", 20), ("", "é", 20), ("", "x" * 2049, 20),
    ("", "", True), ("", "", 0), ("", "", 51), ("", "", "20")])
def test_bad_query_never_reaches_transport(instance, before, limit, monkeypatch):
    client = HubClient("https://hub.example", "token")
    monkeypatch.setattr(client, "_request", lambda *_a, **_k: pytest.fail("unexpected request"))
    with pytest.raises(ValueError):
        client.list_session_operations(instance, before=before, limit=limit)


@pytest.mark.parametrize("changes", [{"actor": "private"}, {"operation": "unknown"},
    {"status": "closed"}, {"status": []}, {"name": "bad\u200bname"}, {"request_id": ""},
    {"session_id": ""}, {"status": "queued", "session_id": "session-1"},
    {"status": "failed", "session_id": "", "error_code": "arbitrary"},
    {"status": "lost", "session_id": "", "error_code": "session_capacity"},
    {"created_at": True}, {"updated_at": -1}, {"created_at": float("inf")},
    {"updated_at": 10 ** 500}])
def test_invalid_records_reject_without_leaking_fields(changes):
    with pytest.raises(HubError, match="invalid session request history") as error:
        operation_record(record(**changes))
    assert error.value.code == "invalid_response" and "private" not in str(error.value)


@pytest.mark.parametrize("changes", [{"name": "not-empty"}, {"status": "created"},
    {"session_id": ""}, {"status": "failed", "error_code": ""}, {"error_code": []}])
def test_invalid_close_records_reject(changes):
    value = close_record()
    value.update(changes)
    with pytest.raises(HubError):
        operation_record(value)


def test_identifiers_keep_compatible_c1_unicode_and_both_operations_can_share_request_id():
    value = record(request_id="id\u0085middle", session_id="s\u009b中")
    assert operation_record(value) == value
    first, second = record(), close_record()
    second["request_id"] = first["request_id"]
    assert len(page({"data": [first, second], "next_cursor": ""})["data"]) == 2


@pytest.mark.parametrize("value", [{"data": []}, {"data": [], "next_cursor": "", "extra": 1},
    {"data": [], "next_cursor": "cursor"}, {"data": [record()], "next_cursor": 1},
    {"data": [record()], "next_cursor": "bad="}, {"data": [record()], "next_cursor": "before"},
    {"data": [record(), record()], "next_cursor": ""},
    {"data": [record(instance_id="beta")], "next_cursor": ""}])
def test_bad_envelopes_instance_and_cursor_fail_closed(value):
    with pytest.raises(HubError):
        page(value, instance_id="alpha", before="before", limit=20)


def test_page_cannot_exceed_requested_limit():
    with pytest.raises(HubError):
        page({"data": [record(), close_record()], "next_cursor": ""}, limit=1)


def test_duplicate_json_fields_are_rejected_on_the_wire(endpoint):
    client, state = endpoint
    state.raw = b'{"data":[],"data":[],"next_cursor":""}'
    with pytest.raises(HubError) as error:
        client.list_session_operations()
    assert error.value.code == "invalid_response"


def test_history_response_has_256kib_cap_without_lowering_other_routes(endpoint):
    client, state = endpoint
    state.body = {"data": {"session_id": "s", "note": "x" * (256 * 1024)}}
    with pytest.raises(HubError) as error:
        client.list_session_operations()
    assert error.value.code == "response_too_large"
    assert client.get_session("s")["session_id"] == "s"
    assert client.max_response_bytes > 256 * 1024


def test_detail_refresh_binds_immutable_name_target_request_and_time():
    prior = record(status="queued", session_id="")
    detail = {key: value for key, value in record().items() if key != "operation"}
    assert refreshed_record(prior, detail)["session_id"] == "session-1"
    for changes in ({"name": "Other"}, {"instance_id": "beta"}, {"request_id": "other"},
                    {"created_at": 9}):
        with pytest.raises(HubError):
            refreshed_record(prior, dict(detail, **changes))
    with pytest.raises(HubError):
        refreshed_record(record(), dict(detail, session_id="different"))
    closed = close_record()
    close_detail = {key: value for key, value in closed.items() if key not in {"operation", "name"}}
    assert refreshed_record(closed, close_detail) == closed
    with pytest.raises(HubError):
        refreshed_record(closed, dict(close_detail, session_id="different"))


def test_auth_context_tracks_rotation_in_memory_only():
    token = ["first"]
    client = HubClient("https://hub.example", token_provider=lambda: token[0])
    before = client._auth_context()
    assert before == client._auth_context() and "first" not in repr(before)
    token[0] = "second"
    assert before != client._auth_context() and "second" not in repr(client)


def test_cli_returns_one_envelope_and_visible_cursor_without_business_replay(monkeypatch, capsys):
    class Client:
        def list_session_operations(self, instance, *, before, limit):
            assert (instance, before, limit) == ("alpha", "cursor", 2)
            return {"data": [record(), close_record()], "next_cursor": "next"}

    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *_a, **_k: Client())
    args = ["session-operations", "--instance", "alpha", "--before", "cursor", "--limit", "2"]
    assert hub_cli.hub_main(args + ["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["next_cursor"] == "next"
    assert hub_cli.hub_main(args) == 0
    text = capsys.readouterr().out
    assert "create created" in text and "close closed" in text and "next_cursor: next" in text


@pytest.mark.parametrize("status,expected", [(401, "fleet auth login"), (403, "Access denied"),
                                          (404, "does not support"), (405, "does not support"),
                                          (501, "does not support"), (503, "refresh to retry")])
def test_cli_history_failure_uses_only_safe_diagnostics(monkeypatch, capsys, status, expected):
    class Client:
        def list_session_operations(self, *_a, **_k):
            raise HubError("PRIVATE response body", status=status)

    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *_a, **_k: Client())
    assert hub_cli.hub_main(["session-operations"]) == 2
    text = capsys.readouterr().err
    assert expected in text and "PRIVATE" not in text


def test_human_cli_escapes_control_and_direction_ids_while_json_preserves_identity(monkeypatch, capsys):
    target = "id\u009bmid\u202etail"

    class Client:
        def list_session_operations(self, *_a, **_k):
            return {"data": [record(request_id=target)], "next_cursor": ""}

    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *_a, **_k: Client())
    assert hub_cli.hub_main(["session-operations"]) == 0
    text = capsys.readouterr().out
    assert target not in text and "id\\u009bmid\\u202etail" in text
    assert hub_cli.hub_main(["session-operations", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"][0]["request_id"] == target


@pytest.mark.parametrize("field", ["request_id", "instance_id", "session_id"])
@pytest.mark.parametrize("surrogate", ["\ud800", "\udc00"])
def test_history_rejects_non_utf8_identifiers_in_both_operations(field, surrogate):
    for value in (record(), close_record()):
        value[field] = surrogate
        with pytest.raises(HubError) as error:
            operation_record(value)
        assert error.value.code == "invalid_response"


@pytest.mark.parametrize("surrogate", ["\ud800", "\udc00"])
def test_history_query_rejects_non_utf8_instance_before_transport(monkeypatch, surrogate):
    client = HubClient("https://hub.example", "token")
    monkeypatch.setattr(client, "_request", lambda *_a, **_k: pytest.fail("unexpected request"))
    with pytest.raises(ValueError, match="invalid history identifier"):
        client.list_session_operations(surrogate)
