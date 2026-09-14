"""Real Hub HTTP/outbox/adapters exercised against local sink HTTP substitutes."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pbatch.hub_auth import GATEWAY_SCOPE, USER_SCOPES, HubAuth
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_integration_cli import main as integrations_cli
from pbatch.hub_integrations import HubIntegrations
from pbatch.hub_store import HubStore

PROMPT_SECRET = "PROMPT_BODY_PRIVATE_4bb6"
OUTPUT_SECRET = "OUTPUT_BODY_PRIVATE_81d1"
AUDIT_TIME = "2026-09-10T00:26:40Z"
ZERO_TIME = "0001-01-01T00:00:00Z"


def _grant(token, *, tenant="team", kind="user", instances=("alpha",), scopes=None):
    return {
        "subject": token, "client_id": token, "tenant_id": tenant, "kind": kind,
        "instances": list(instances), "scopes": list(USER_SCOPES if scopes is None else scopes),
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
    }


class _SinkState:
    def __init__(self):
        self.lock = threading.Lock()
        self.audit_calls = []
        self.im_calls = []
        self.audit_counts = {}
        self.im_counts = {}
        self.script_retries = False
        self.bad_audit_kinds = set()
        self.bad_audit_consumed = set()

    def audit(self, body, authorization):
        event_id = body["event_id"]
        kind = body["payload"]["kind"]
        with self.lock:
            count = self.audit_counts.get(event_id, 0) + 1
            self.audit_counts[event_id] = count
            self.audit_calls.append({"body": copy.deepcopy(body), "authorization": authorization})
            if self.script_retries and kind == "turn.queued" and count == 1:
                return "drop", None
            if self.script_retries and kind == "turn.queued" and count == 2:
                status = "accepted"
            else:
                status = "ledgered"
            receipt = _audit_receipt(body, status, duplicate=count > 1)
            if kind in self.bad_audit_kinds and kind not in self.bad_audit_consumed:
                self.bad_audit_consumed.add(kind)
                receipt["event_id"] = "wrong-event-id"
            return "json", {"receipt": receipt,
                             "receipt_url": f"/api/v1/events/{body['event_id']}/receipt"}

    def im(self, body, idem, authorization):
        with self.lock:
            count = self.im_counts.get(idem, 0) + 1
            self.im_counts[idem] = count
            self.im_calls.append({"body": copy.deepcopy(body), "idem": idem,
                                  "authorization": authorization})
            if self.script_retries and count == 1:
                return 409, {"error": {"code": "integration_request_pending"}}
            if self.script_retries and count == 2:
                return "drop", None
            return 200, {"message": {"id": "message-" + idem},
                         "deduplicated": count > 1}


def _audit_receipt(body, status, *, duplicate):
    event_id = body["event_id"]
    receipt = {
        "event_id": event_id, "tenant_id": body["tenant_id"],
        "idempotency_key": body["idempotency_key"], "status": status,
        "accepted_at": AUDIT_TIME, "stream_id": "team:source:agent-hub",
        "sequence": 1, "hash": hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }
    if status == "accepted":
        receipt.update(ledgered_at=ZERO_TIME, indexed_at=ZERO_TIME, archived_at=ZERO_TIME)
    else:
        receipt["ledgered_at"] = AUDIT_TIME
    if duplicate:
        receipt["duplicate"] = True
    return receipt


def _start_sink_server(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size).decode())
            if self.path == "/api/v1/events?wait_for=ledgered":
                action, response = state.audit(body, self.headers.get("Authorization", ""))
                status = 202
            elif self.path.startswith("/api/integrations/v1/installations/"):
                action, response = state.im(body, self.headers.get("Idempotency-Key", ""),
                                            self.headers.get("Authorization", ""))
                status = action if isinstance(action, int) else 200
            else:
                action, response, status = "json", {"error": "unknown path"}, 404
            if action == "drop":
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            if isinstance(action, int):
                status = action
            self._json(status, response)

        def _json(self, status, value):
            raw = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def handle_error(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def ecosystem(tmp_path):
    clock = [1_000.0]
    external = _SinkState()
    sink_server, sink_thread, sink_url = _start_sink_server(external)
    token_files = {}
    for name, secret in (("audit", "audit-service-secret"), ("im", "im-service-secret")):
        token = tmp_path / f"{name}.token"
        token.write_text(secret, encoding="utf-8")
        token.chmod(0o600)
        token_files[name] = str(token)
    hub_store = HubStore(tmp_path / "hub.sqlite3", clock=lambda: clock[0])
    settings = [
        {"kind": "audit", "name": "audit-sink", "tenant_id": "team",
         "base_url": sink_url, "token_file": token_files["audit"],
         "source_system": "agent-hub"},
        {"kind": "im", "name": "im-sink", "tenant_id": "team",
         "base_url": sink_url, "token_file": token_files["im"],
         "installation_id": "11111111-1111-4111-8111-111111111111",
         "recipient_subject_map": {"alice": "chat-user-1"}},
    ]
    integrations = HubIntegrations(hub_store, settings)
    scopes = {"agent.integrations:read", "agent.integrations:retry"}
    grants = [
        _grant("alice"),
        _grant("gateway-alpha", kind="gateway", scopes=[GATEWAY_SCOPE]),
        _grant("read-only", scopes=["agent.integrations:read"]),
        _grant("no-read", scopes=["agent.instances:read"]),
        _grant("beta-reader", instances=("beta",), scopes=["agent.integrations:read"]),
        _grant("beta-retry", instances=("beta",), scopes=scopes),
        _grant("other-tenant", tenant="other", scopes=scopes),
    ]
    auth = HubAuth({"mode": "development", "grants": grants})
    hub_server = HubHTTPServer(("127.0.0.1", 0), hub_store, auth,
                               integrations=integrations)
    hub_thread = threading.Thread(target=hub_server.serve_forever, daemon=True)
    hub_thread.start()
    hub_url = f"http://127.0.0.1:{hub_server.server_port}"
    clients = {token: HubClient(hub_url, token) for token in (
        "alice", "gateway-alpha", "read-only", "no-read", "beta-reader", "beta-retry",
        "other-tenant")}
    value = type("Ecosystem", (), {})()
    value.clock, value.external = clock, external
    value.hub_store, value.integrations, value.hub_server = hub_store, integrations, hub_server
    value.hub_url, value.clients = hub_url, clients
    yield value
    hub_server.shutdown()
    hub_server.server_close()
    hub_thread.join(timeout=5)
    integrations.close()
    hub_store.close()
    sink_server.shutdown()
    sink_server.server_close()
    sink_thread.join(timeout=5)


def _register_session(ecosystem):
    gateway = ecosystem.clients["gateway-alpha"]
    instance = gateway.register_instance(instance_id="alpha", boot_id="boot-1",
                                         name="Alpha", project_id="demo")
    session = gateway.register_session(instance_id="alpha", generation=instance["generation"],
                                       local_session_id="local-1", name="Work",
                                       project_id="demo")
    return gateway, ecosystem.clients["alice"], instance["generation"], session["session_id"]


def _delivery_rows(ecosystem):
    return ecosystem.integrations.store.list("team", None, limit=200)


def _delivery(rows, sink_kind, kind):
    return next(row for row in rows
                if row["sink_kind"] == sink_kind and row["kind"] == kind)


def test_http_lifecycle_retries_to_audit_and_im_without_exporting_bodies(ecosystem):
    ecosystem.external.script_retries = True
    gateway, user, generation, session_id = _register_session(ecosystem)
    turn = user.send_prompt(session_id, PROMPT_SECRET, "prompt-key-1")
    turn_id = turn["turn_id"]
    commands = gateway.get_commands(instance_id="alpha", generation=generation)
    assert commands[0]["prompt"] == PROMPT_SECRET
    gateway.send_event({"event_id": "running-1", "session_id": session_id,
                        "turn_id": turn_id, "kind": "turn.running", "payload": {}},
                       instance_id="alpha", generation=generation)
    gateway.send_event({"event_id": "completed-1", "session_id": session_id,
                        "turn_id": turn_id, "kind": "turn.completed",
                        "payload": {"text": OUTPUT_SECRET}},
                       instance_id="alpha", generation=generation)
    assert user.get_turn(turn_id)["state"] == "completed"
    assert any(event["payload"].get("text") == OUTPUT_SECRET
               for event in user.session_events(session_id))

    ecosystem.integrations.tick()
    rows = _delivery_rows(ecosystem)
    queued = _delivery(rows, "audit", "turn.queued")
    completed_im = _delivery(rows, "im", "turn.completed")
    assert (queued["state"], queued["attempts"], queued["error_code"]) == (
        "retrying", 1, "transport_failed")
    assert (completed_im["state"], completed_im["attempts"]) == ("retrying", 1)

    public, _ = user._request("GET", "/integrations/deliveries?limit=100", include_cursor=True)
    assert len(public) == len(rows)
    assert all("fact" not in row and "payload" not in row for row in public)
    assert user._request("GET", "/integrations/deliveries?limit=100")[0]["state"] == "retrying"

    for _ in range(4):
        queued_pending = ecosystem.integrations.store.get("team", queued["delivery_id"])
        if queued_pending["attempts"] == 2:
            break
        ecosystem.clock[0] += 400
        ecosystem.integrations.tick()
    queued_pending = ecosystem.integrations.store.get("team", queued["delivery_id"])
    assert (queued_pending["state"], queued_pending["attempts"],
            queued_pending["error_code"], queued_pending["receipt"]) == (
        "retrying", 2, "ledger_pending", {})
    assert completed_im["delivery_id"] in {row["delivery_id"] for row in _delivery_rows(ecosystem)}

    ecosystem.clock[0] += 400
    ecosystem.integrations.tick()
    rows = _delivery_rows(ecosystem)
    assert all(row["state"] == "delivered" for row in rows)
    audit_calls = ecosystem.external.audit_calls
    queued_calls = [call["body"] for call in audit_calls
                    if call["body"]["payload"]["kind"] == "turn.queued"]
    assert len(queued_calls) == 3 and queued_calls[0] == queued_calls[1] == queued_calls[2]
    im_calls = ecosystem.external.im_calls
    assert len(im_calls) == 3
    assert all(call["body"] == im_calls[0]["body"] and call["idem"] == im_calls[0]["idem"]
               for call in im_calls)
    outbound = json.dumps([call["body"] for call in audit_calls] +
                          [call["body"] for call in im_calls])
    assert PROMPT_SECRET not in outbound and OUTPUT_SECRET not in outbound
    assert all(call["authorization"] == "Bearer audit-service-secret" for call in audit_calls)
    assert all(call["authorization"] == "Bearer im-service-secret" for call in im_calls)


def test_delivery_scopes_tenant_paging_cli_and_manual_retry(ecosystem, monkeypatch, capsys):
    ecosystem.external.bad_audit_kinds.add("turn.failed")
    gateway, user, generation, session_id = _register_session(ecosystem)
    turn = user.send_prompt(session_id, "fail safely", "prompt-key-2")
    gateway.send_event({"event_id": "running-2", "session_id": session_id,
                        "turn_id": turn["turn_id"], "kind": "turn.running", "payload": {}},
                       instance_id="alpha", generation=generation)
    gateway.send_event({"event_id": "failed-2", "session_id": session_id,
                        "turn_id": turn["turn_id"], "kind": "turn.failed",
                        "payload": {"text": OUTPUT_SECRET}},
                       instance_id="alpha", generation=generation)
    ecosystem.integrations.tick()
    ecosystem.integrations.tick()
    ecosystem.integrations.tick()
    rows = _delivery_rows(ecosystem)
    failed = _delivery(rows, "audit", "turn.failed")
    assert failed["state"] == "failed" and failed["error_code"] == "receipt_mismatch"
    assert user.get_turn(turn["turn_id"])["state"] == "failed"

    readonly, denied = ecosystem.clients["read-only"], ecosystem.clients["no-read"]
    listed, _ = readonly._request("GET", "/integrations/deliveries?limit=100", include_cursor=True)
    assert listed and any(row["delivery_id"] == failed["delivery_id"] for row in listed)
    with pytest.raises(HubError) as caught:
        denied._request("GET", "/integrations/deliveries?limit=10")
    assert caught.value.status == 403
    with pytest.raises(HubError) as caught:
        readonly._request("POST", f"/integrations/deliveries/{failed['delivery_id']}/retry",
                          {}, accepted=(202,))
    assert caught.value.status == 403
    beta = ecosystem.clients["beta-reader"]
    assert beta._request("GET", "/integrations/deliveries?limit=10") == []
    with pytest.raises(HubError) as caught:
        ecosystem.clients["beta-retry"]._request(
            "POST", f"/integrations/deliveries/{failed['delivery_id']}/retry", {}, accepted=(202,))
    assert caught.value.status == 403
    other = ecosystem.clients["other-tenant"]
    assert other._request("GET", "/integrations/deliveries?limit=10") == []
    with pytest.raises(HubError) as caught:
        other._request("POST", f"/integrations/deliveries/{failed['delivery_id']}/retry", {},
                       accepted=(202,))
    assert caught.value.status == 404

    monkeypatch.setenv("PBATCH_HUB_URL", ecosystem.hub_url)
    monkeypatch.setenv("PBATCH_HUB_TOKEN", "alice")
    assert integrations_cli(["deliveries", "--limit", "1"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert len(first["data"]) == 1 and first["next_cursor"]
    assert integrations_cli(["deliveries", "--after", str(first["next_cursor"]),
                             "--limit", "1"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert len(second["data"]) == 1
    assert first["data"][0]["delivery_id"] != second["data"][0]["delivery_id"]

    calls_before = len([call for call in ecosystem.external.audit_calls
                        if call["body"]["payload"]["kind"] == "turn.failed"])
    assert integrations_cli(["retry", failed["delivery_id"]]) == 0
    retried = json.loads(capsys.readouterr().out)
    assert retried["state"] == "pending"
    ecosystem.integrations.tick()
    final = ecosystem.integrations.store.get("team", failed["delivery_id"])
    assert final["state"] == "delivered"
    failed_calls = [call["body"] for call in ecosystem.external.audit_calls
                    if call["body"]["payload"]["kind"] == "turn.failed"]
    assert len(failed_calls) == calls_before + 1
    assert failed_calls[-1] == failed_calls[-2]
    assert OUTPUT_SECRET not in json.dumps(failed_calls)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
