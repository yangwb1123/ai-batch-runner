import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from pbatch import hub_cli
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_session_client import creation_record


def request_record(**values):
    return dict(request_id="request-1", instance_id="alpha", name="Analysis",
                status="queued", session_id="", error_code="", created_at=1,
                updated_at=1, **values)


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
            body = json.loads(raw) if raw else None
            seen.append((self.command, self.path, dict(self.headers), body))
            result = request_record()
            status = 200
            if self.path.endswith("/alpha/sessions"):
                status = 202
            elif self.path.endswith("/gateway/register"):
                result = {"generation": 1}
            elif urlsplit(self.path).path.endswith("/gateway/session-requests"):
                if self.command == "GET":
                    result = [{"request_id": "request-1", "generation": 2}]
                else:
                    result.update(status="created", session_id="session-1")
            encoded = json.dumps({"data": result}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield HubClient(f"http://127.0.0.1:{server.server_port}", "test-bearer"), seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_user_creation_posts_once_and_gets_the_same_request(endpoint):
    client, seen = endpoint
    request = client.create_session("alpha", " Analysis ", idempotency_key="stable-1")
    assert request == client.session_request("request-1")
    assert [row[0] for row in seen] == ["POST", "GET"]
    assert seen[0][1] == "/api/v1/agent/instances/alpha/sessions"
    assert seen[0][2]["Idempotency-Key"] == "stable-1"
    assert seen[0][3] == {"name": "Analysis"}
    assert seen[1][1] == "/api/v1/agent/session-requests/request-1"
    assert seen[1][3] is None


def test_gateway_capacity_and_creation_routes(endpoint):
    client, seen = endpoint
    client.register_instance(instance_id="alpha", boot_id="boot", name="Alpha",
                             project_id="demo", session_capacity=8)
    assert seen[-1][3]["session_capacity"] == 8
    rows = client.get_session_requests(instance_id="alpha", generation=2)
    assert rows[0]["request_id"] == "request-1"
    assert parse_qs(urlsplit(seen[-1][1]).query) == {"instance_id": ["alpha"], "generation": ["2"]}
    result = client.report_session_request(instance_id="alpha", generation=2,
                                            request_id="request-1", status="created", error_code="")
    assert result["session_id"] == "session-1"
    assert seen[-1][3] == {"instance_id": "alpha", "generation": 2,
                           "request_id": "request-1", "status": "created", "error_code": ""}


@pytest.mark.parametrize("name", ["", "  ", "a\nb", "a\x00b", "a\u202eb", "中" * 86, 5])
def test_invalid_creation_name_never_sends(name, monkeypatch):
    client = HubClient("https://hub.example", "token")
    monkeypatch.setattr(client, "_request", lambda *_a, **_k: pytest.fail("unexpected request"))
    with pytest.raises(ValueError):
        client.create_session("alpha", name, idempotency_key="key")


@pytest.mark.parametrize("key", ["", "a b", "a\rb", "中", "k" * 257])
def test_invalid_creation_key_never_sends(key, monkeypatch):
    client = HubClient("https://hub.example", "token")
    monkeypatch.setattr(client, "_request", lambda *_a, **_k: pytest.fail("unexpected request"))
    with pytest.raises(ValueError):
        client.create_session("alpha", "Analysis", idempotency_key=key)


@pytest.mark.parametrize("changes", [
    {"status": "running"}, {"status": "created"}, {"session_id": "unexpected"},
    {"status": "failed", "error_code": ""}, {"created_at": float("nan")},
    {"updated_at": True}, {"request_id": ""}, {"name": "unsafe\nname"},
])
def test_creation_response_cannot_become_a_false_success(changes):
    record = request_record()
    record.update(changes)
    with pytest.raises(HubError, match="invalid session creation"):
        creation_record(record)


def test_creation_response_identity_is_bound_to_the_request(monkeypatch):
    client = HubClient("https://hub.example", "token")
    monkeypatch.setattr(client, "_request", lambda *_a, **_k: request_record())
    with pytest.raises(HubError):
        client.create_session("beta", "Analysis", idempotency_key="key")
    with pytest.raises(HubError):
        client.session_request("another-request")


def test_cli_emits_key_before_submission_and_supports_status(monkeypatch, capsys):
    class Client:
        def create_session(self, instance_id, name, *, idempotency_key):
            captured = capsys.readouterr()
            assert "Idempotency-Key: saved-key" in captured.err
            assert (instance_id, name, idempotency_key) == ("alpha", "Analysis", "saved-key")
            return request_record()

        def session_request(self, request_id):
            assert request_id == "request-1"
            return request_record()

    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *_a, **_k: Client())
    assert hub_cli.hub_main(["session-create", "alpha", "--name", "Analysis",
                              "--idempotency-key", "saved-key", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["request_id"] == "request-1"
    assert hub_cli.hub_main(["session-request", "request-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
