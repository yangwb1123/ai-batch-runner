"""User/Gateway wire protocol, invalid replies and recoverable CLI request keys."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from pbatch import hub_cli
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_session_close_client import close_record


def record(**changes):
    return dict(dict(request_id="close-1", instance_id="alpha", session_id="session-1",
                     status="queued", error_code="", created_at=1, updated_at=1), **changes)


@pytest.fixture
def endpoint():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append((self.command, self.path, dict(self.headers), json.loads(raw) if raw else None))
            result, status = record(), 200
            if self.path.endswith("/sessions/session-1/close"):
                status = 202
            if self.path.endswith("/gateway/register"):
                result = {"generation": 1}
            if urlsplit(self.path).path.endswith("/gateway/session-close-requests"):
                result = [record()] if self.command == "GET" else record(status="closed")
            data = json.dumps({"data": result}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield HubClient(f"http://127.0.0.1:{server.server_port}", "test-token"), seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_close_uses_an_empty_body_and_preserves_idempotency_key(endpoint):
    client, seen = endpoint
    result = client.close_session("session-1", idempotency_key="same-close")
    assert client.session_close_request(result["request_id"]) == result
    assert [row[0] for row in seen] == ["POST", "GET"]
    assert seen[0][1] == "/api/v1/agent/sessions/session-1/close"
    assert seen[0][2]["Idempotency-Key"] == "same-close" and seen[0][3] == {}
    assert seen[1][1] == "/api/v1/agent/session-close-requests/close-1" and seen[1][3] is None


def test_gateway_advertisement_and_close_reports(endpoint):
    client, seen = endpoint
    values = dict(instance_id="alpha", boot_id="boot", name="Alpha", project_id="p")
    client.register_instance(**values)
    assert "session_close_supported" not in seen[-1][3]
    client.register_instance(**values, session_close_supported=True)
    assert seen[-1][3]["session_close_supported"] is True
    assert len(client.get_session_close_requests(instance_id="alpha", generation=1)) == 1
    assert parse_qs(urlsplit(seen[-1][1]).query) == {"instance_id": ["alpha"], "generation": ["1"]}
    assert client.report_session_close_request(instance_id="alpha", generation=1,
        request_id="close-1", status="closed", error_code="")["status"] == "closed"
    assert seen[-1][3] == dict(instance_id="alpha", generation=1, request_id="close-1",
                              status="closed", error_code="")


@pytest.mark.parametrize("changes", [
    {"request_id": ""}, {"session_id": ""}, {"instance_id": "other"}, {"status": "created"},
    {"status": "failed", "error_code": ""}, {"status": "closed", "error_code": "failure"},
    {"status": "lost", "error_code": "made-up"}, {"created_at": True},
    {"updated_at": float("nan")}, {"updated_at": -1}, {"updated_at": 10 ** 500},
    {"status": []}, {"error_code": {}},
])
def test_invalid_close_response_never_confirms_success(changes):
    with pytest.raises(HubError, match="invalid session close"):
        close_record(record(**changes), instance_id="alpha", session_id="session-1")


@pytest.mark.parametrize("session,key", [("", "key"), (" s", "key"), ("s\n", "key"),
                                          ("s", ""), ("s", "a b"), ("s", "中")])
def test_invalid_close_input_never_posts(session, key, monkeypatch):
    client = HubClient("https://hub.example", "token")
    monkeypatch.setattr(client, "_request", lambda *_a, **_k: pytest.fail("unexpected request"))
    with pytest.raises(ValueError):
        client.close_session(session, idempotency_key=key)


def test_close_response_identity_and_capability_are_checked(monkeypatch):
    client = HubClient("https://hub.example", "token")
    monkeypatch.setattr(client, "_request", lambda *_a, **_k: record(actor="private"))
    assert "actor" not in client.session_close_request("close-1")
    with pytest.raises(HubError):
        client.close_session("other-session", idempotency_key="key")
    with pytest.raises(HubError):
        client.session_close_request("other-request")
    with pytest.raises(ValueError):
        client.register_instance(instance_id="alpha", boot_id="boot", name="a", project_id="p",
                                 session_close_supported=1)


def test_cli_prints_key_before_post_and_can_inspect_close(monkeypatch, capsys):
    class Client:
        def close_session(self, session_id, *, idempotency_key):
            assert "Idempotency-Key: recover-key" in capsys.readouterr().err
            assert (session_id, idempotency_key) == ("session-1", "recover-key")
            return record()

        def session_close_request(self, request_id):
            assert request_id == "close-1"
            return record(status="closed")

    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *_a, **_k: Client())
    assert hub_cli.hub_main(["session-close", "session-1", "--idempotency-key", "recover-key", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
    assert hub_cli.hub_main(["session-close-request", "close-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "closed"


def test_cli_busy_and_authorization_errors_are_fixed_safe_messages(monkeypatch, capsys):
    class Client:
        error = HubError("SECRET upstream body", code="session_busy", status=409)

        def close_session(self, *_args, **_kwargs):
            raise self.error

    client = Client()
    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *_a, **_k: client)
    args = ["session-close", "session-1", "--idempotency-key", "key"]
    assert hub_cli.hub_main(args) == 2
    output = capsys.readouterr().err
    assert "SECRET" not in output and "active prompt or computation" in output
    client.error = HubError("SECRET", status=401)
    assert hub_cli.hub_main(args) == 2
    output = capsys.readouterr().err
    assert "SECRET" not in output and "fleet auth login" in output
