"""Strict single-page diagnostics over real bounded HTTP and CLI transports."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from pbatch import hub_compute_cli
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_compute_client import ComputeClient
from pbatch.hub_compute_placement_client import decode, parameters, placement


def device(**changes):
    return dict(dict(device_id="device-b", instance_id="remote", eligible=True, reasons=[]), **changes)


def observation(**changes):
    return dict(dict(task_id="task-1", state="queued", evaluated_at=1.5,
                     devices=[device()], next_cursor="device-b"), **changes)


@pytest.fixture
def endpoint():
    state = SimpleNamespace(body={"data": observation()}, raw=None, status=200, calls=[],
                            mutate=None, length=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.calls.append((self.command, self.path, dict(self.headers), body))
            raw = state.raw if state.raw is not None else json.dumps(state.body).encode()
            if state.mutate:
                state.mutate()
            self.send_response(state.status)
            if state.length:
                self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield ComputeClient(HubClient(f"http://127.0.0.1:{server.server_port}", "fake-token")), state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_read_one_page_binds_task_and_never_submits(endpoint):
    client, state = endpoint
    assert client.placement("task-1", after="device-a", limit=1) == state.body["data"]
    assert len(state.calls) == 1
    method, path, headers, body = state.calls[0]
    assert method == "GET" and urlsplit(path).path == "/api/v1/agent/tasks/task-1/placement"
    assert parse_qs(urlsplit(path).query) == {"after": ["device-a"], "limit": ["1"]}
    assert body == b"" and "Idempotency-Key" not in headers


@pytest.mark.parametrize("task,after,limit", [
    ("", "", 20), (" x", "", 20), ("a\n", "", 20), ("a" * 257, "", 20),
    ("\ud800", "", 20), (None, "", 20), ("task-1", " a", 20),
    ("task-1", "\udfff", 20), ("task-1", None, 20), ("task-1", [], 20),
    ("task-1", "", True), ("task-1", "", 0), ("task-1", "", 21),
    ("task-1", "", "20"), ("task-1", "", 1.0),
])
def test_bad_query_rejected_without_network(task, after, limit, monkeypatch):
    client = HubClient("https://hub.example", "fake-token")
    monkeypatch.setattr(client, "_request", lambda *_a, **_k: pytest.fail("unexpected HTTP request"))
    with pytest.raises(ValueError):
        ComputeClient(client).placement(task, after=after, limit=limit)


@pytest.mark.parametrize("changes", [
    {"private": "secret"}, {"task_id": "other"}, {"state": "future"}, {"state": []},
    {"evaluated_at": True}, {"evaluated_at": float("inf")}, {"evaluated_at": float("nan")},
    {"evaluated_at": -1}, {"evaluated_at": 10 ** 500}, {"devices": None},
    {"devices": [device()] * 21}, {"devices": [device(), device()]},
    {"devices": [device(device_id="c"), device(device_id="b")]},
    {"devices": [], "next_cursor": "other"}, {"next_cursor": "other"},
    {"next_cursor": None}, {"next_cursor": []}, {"state": "completed"},
])
def test_exact_dto_rejects_invalid_shape_state_and_pagination(changes):
    with pytest.raises(HubError, match="invalid compute placement"):
        placement(observation(**changes), task_id="task-1")


@pytest.mark.parametrize("changes", [
    {"raw_fabric": "secret"}, {"device_id": ""}, {"device_id": "\udfff"},
    {"instance_id": " x"}, {"instance_id": None}, {"eligible": 1}, {"eligible": False},
    {"reasons": None}, {"reasons": ["invented"]}, {"reasons": [[]]},
    {"eligible": False, "reasons": ["device_busy", "device_busy"]},
    {"reasons": ["device_busy"]},
])
def test_exact_device_and_eligibility_reason_invariants(changes):
    with pytest.raises(HubError):
        placement(observation(devices=[device(**changes)]), task_id="task-1")


@pytest.mark.parametrize("raw", [
    b'{"data":{},"data":{}}', b'{"data":{},"extra":1}', b'[]', b'\xff',
    b'{"data":{"task_id":"task-1","task_id":"task-1"}}', b'[' * 2000,
])
def test_wire_parser_rejects_duplicate_fields_and_invalid_envelope(raw):
    with pytest.raises(HubError):
        decode(raw, task_id="task-1")


def test_utf8_cursor_identifiers_and_nonqueued_empty_pages():
    value = observation(devices=[device(device_id="节\u0080点")], next_cursor="节\u0080点")
    assert placement(value, task_id="task-1") == value
    for state in ("dispatching", "running", "cancel_requested", "completed", "failed", "cancelled", "lost"):
        value = observation(state=state, devices=[], next_cursor="")
        assert placement(value, task_id="task-1") == value
    with pytest.raises(HubError):
        placement(observation(), task_id="task-1", after="device-b")


@pytest.mark.parametrize("length", [True, False])
def test_transport_caps_responses_at_64_kib(endpoint, length):
    client, state = endpoint
    state.length = length
    state.raw = b" " * (64 * 1024 + 1)
    with pytest.raises(HubError) as error:
        client.placement("task-1")
    assert error.value.code == "response_too_large"


def test_identity_change_during_http_discards_success(endpoint):
    client, state = endpoint
    state.mutate = lambda: setattr(client.client, "_token", "replacement")
    with pytest.raises(HubError) as error:
        client.placement("task-1")
    assert error.value.code == "identity_changed"


def test_gateway_read_validates_payload_key_and_exact_response(endpoint):
    client, state = endpoint
    context = {"instance_id": "source", "generation": 1, "session_id": "session", "turn_id": "turn"}
    assert client.delegate(context, "placement", {"task_id": "task-1"}) == state.body["data"]
    method, path, headers, body = state.calls[0]
    assert method == "POST" and path.endswith("/gateway/compute")
    assert json.loads(body) == dict(context, operation="placement", key="",
                                    payload=parameters("task-1"))
    assert "Idempotency-Key" not in headers
    for payload, key in [({"task_id": "task-1", "extra": True}, ""), ({}, ""),
                         ({"task_id": "task-1"}, "not-empty")]:
        with pytest.raises(ValueError):
            client.delegate(context, "placement", payload, key)
    assert len(state.calls) == 1


def test_gateway_response_is_also_bounded(endpoint):
    client, state = endpoint
    state.raw = b" " * (64 * 1024 + 1)
    with pytest.raises(HubError) as error:
        client.delegate({"instance_id": "source"}, "placement", {"task_id": "task-1"})
    assert error.value.code == "response_too_large"


def test_existing_smaller_client_limit_is_preserved(endpoint):
    client, state = endpoint
    client.client.max_response_bytes = 1024
    state.raw = b" " * 1025
    with pytest.raises(HubError) as error:
        client.placement("task-1")
    assert error.value.code == "response_too_large"


def test_cli_direct_human_and_json_rendering(endpoint, monkeypatch, capsys):
    client, state = endpoint
    state.body = {"data": observation(devices=[device(instance_id="remote\u0080\u200b")])}
    monkeypatch.setattr(HubClient, "from_env", lambda *_a, **_k: client.client)
    assert hub_compute_cli.main(["placement", "task-1", "--limit", "2"]) == 0
    human = capsys.readouterr().out
    assert "remote\\u0080\\u200b" in human and "Fabric still decides" in human
    assert hub_compute_cli.main(["placement", "task-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == state.body["data"]


@pytest.mark.parametrize("status", [404, 405, 501])
def test_cli_old_hub_reports_unsupported(endpoint, monkeypatch, capsys, status):
    client, state = endpoint
    state.status, state.body = status, {"error": {"code": "not_found", "message": "old hub"}}
    monkeypatch.setattr(HubClient, "from_env", lambda *_a, **_k: client.client)
    assert hub_compute_cli.main(["placement", "task-1"]) == 2
    assert "does not support" in capsys.readouterr().err
