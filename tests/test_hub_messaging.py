"""Contract tests for the Agent Hub to Aero IM notification sink."""

from __future__ import annotations

import unittest
import uuid
import http.server
import json
import tempfile
import threading
from pathlib import Path

from pbatch.hub_ecosystem_http import IntegrationError
from pbatch.hub_messaging import MessagingSink


class _Transport:
    def __init__(self, result=None, error=None):
        self.result = result or {
            "message": {"id": "message-123"}, "deduplicated": False,
        }
        self.error = error
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.error:
            raise self.error
        return self.result


class _ImServerState:
    def __init__(self):
        self.responses = []
        self.requests = []


class _ImHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        request = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "idempotency_key": self.headers.get("Idempotency-Key"),
            "body": json.loads(self.rfile.read(size)),
        }
        self.server.state.requests.append(request)
        status, body = self.server.state.responses.pop(0)
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class HubMessagingTests(unittest.TestCase):
    def setUp(self):
        self.sink = MessagingSink({
            "tenant_id": "tenant-acme",
            "base_url": "https://im.example.test",
            "installation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "token_file": "/private/oidc-token",
            "recipient_subject_map": {"hub-subject": "im-pairwise-subject"},
        })
        self.transport = _Transport()
        self.sink.transport = self.transport

    @staticmethod
    def _fact(**changes):
        fact = {
            "tenant_id": "tenant-acme",
            "event_id": "event-terminal-42",
            "cursor": 42,
            "kind": "compute.completed",
            "created_at": 1_789_000_000.0,
            "instance_id": "device-west",
            "session_id": "session-global-1",
            "turn_id": "turn-9",
            "task_id": "compute-7",
            "actor_subject": "hub-subject",
            "status": "completed",
        }
        fact.update(changes)
        return fact

    def test_prepare_freezes_mapped_subject_and_minimal_status_only_card(self):
        payload = self.sink.prepare(self._fact(
            prompt="must-not-leak", output="also-private", reason="private error",
            trigger_subject="",
        ))
        self.assertEqual(payload["target"], {
            "type": "snaplink_user", "subject": "im-pairwise-subject",
        })
        self.assertEqual(payload["expected_subject"], "im-pairwise-subject")
        self.assertEqual(payload["actor_subject"], "hub-subject")
        text = payload["blocks"][0]["content"]
        self.assertIn("Agent Hub task completed", text)
        self.assertIn("Task: compute-7", text)
        self.assertIn("Session: session-global-1", text)
        self.assertIn("Event: event-terminal-42", text)
        self.assertNotIn("must-not-leak", text)
        self.assertNotIn("private error", text)
        self.assertNotIn("trigger_subject", text)

    def test_nonterminal_fact_is_ignored(self):
        self.assertIsNone(self.sink.prepare(self._fact(kind="compute.running", status="running")))
        self.assertIsNone(self.sink.prepare(self._fact(kind="turn.started", status="running")))

    def test_failed_turn_card_uses_only_hub_location_fields(self):
        payload = self.sink.prepare(self._fact(
            kind="turn.failed", status="failed", task_id="", turn_id="turn-10",
            prompt="not a notification field", reason="private failure text",
        ))
        text = payload["blocks"][0]["content"]
        self.assertIn("Agent Hub turn failed", text)
        self.assertIn("Turn: turn-10", text)
        self.assertNotIn("Task:", text)
        self.assertNotIn("private failure text", text)

    def test_cancelled_and_lost_turns_are_not_dropped(self):
        for status in ("cancelled", "lost"):
            with self.subTest(status=status):
                payload = self.sink.prepare(self._fact(
                    kind="turn." + status, status=status, task_id="", turn_id="turn-11",
                ))
                self.assertIn("Agent Hub turn " + status, payload["blocks"][0]["content"])

    def test_unmapped_actor_and_cross_tenant_fact_fail_closed(self):
        with self.assertRaises(IntegrationError) as unmapped:
            self.sink.prepare(self._fact(actor_subject="unknown-hub-subject"))
        self.assertEqual(unmapped.exception.code, "recipient_unmapped")
        self.assertFalse(unmapped.exception.retryable)
        with self.assertRaises(IntegrationError) as tenant:
            self.sink.prepare(self._fact(tenant_id="tenant-other"))
        self.assertEqual(tenant.exception.code, "tenant_mismatch")
        self.assertFalse(tenant.exception.retryable)

    def test_malformed_terminal_status_is_permanent(self):
        with self.assertRaises(IntegrationError) as raised:
            self.sink.prepare(self._fact(kind="compute.failed", status="completed"))
        self.assertEqual(raised.exception.code, "invalid_fact")
        self.assertFalse(raised.exception.retryable)

    def test_delivery_posts_only_public_body_and_uses_stable_key(self):
        payload = self.sink.prepare(self._fact())
        delivery_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        receipt = self.sink.deliver(payload, delivery_id)
        self.assertEqual(receipt, {"message_id": "message-123", "deduplicated": False})
        method, path, kwargs = self.transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/integrations/v1/installations/"
                         "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/notifications")
        self.assertEqual(kwargs["headers"], {"Idempotency-Key": delivery_id})
        self.assertEqual(kwargs["expected"], (200, 201))
        self.assertEqual(kwargs["retry_codes"], ("integration_request_pending",))
        self.assertEqual(set(kwargs["body"]), {"target", "blocks"})
        self.assertEqual(kwargs["body"]["target"]["subject"], "im-pairwise-subject")
        self.assertNotIn("actor_subject", kwargs["body"])
        self.assertNotIn("prompt", kwargs["body"])

    def test_changed_subject_binding_never_silently_redirects_pending_event(self):
        payload = self.sink.prepare(self._fact())
        self.sink.recipient_subject_map["hub-subject"] = "new-im-subject"
        with self.assertRaises(IntegrationError) as raised:
            self.sink.deliver(payload, str(uuid.uuid4()))
        self.assertEqual(raised.exception.code, "recipient_binding_changed")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(self.transport.calls, [])

    def test_delivery_errors_remain_structured_for_outbox_retry(self):
        pending = IntegrationError("http_error", status=409, retryable=True)
        self.sink.transport = _Transport(error=pending)
        with self.assertRaises(IntegrationError) as raised:
            self.sink.deliver(self.sink.prepare(self._fact()), str(uuid.uuid4()))
        self.assertIs(raised.exception, pending)

    def test_fingerprint_is_bound_to_tenant_endpoint_and_installation(self):
        original = self.sink.fingerprint
        same = MessagingSink({
            "tenant_id": "tenant-acme", "base_url": "https://im.example.test/",
            "installation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "token_file": "/rotated/credential-file",
            "recipient_subject_map": {"hub-subject": "other-pairwise-subject"},
        })
        self.assertEqual(original, same.fingerprint)
        self.assertEqual(same.name, "aero-im")
        changed = MessagingSink({
            "tenant_id": "tenant-acme", "base_url": "https://other.example.test",
            "installation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "token_file": "/private/oidc-token",
            "recipient_subject_map": {"hub-subject": "im-pairwise-subject"},
        })
        self.assertNotEqual(original, changed.fingerprint)

    def test_prepared_payload_shape_and_receipt_are_validated(self):
        payload = self.sink.prepare(self._fact())
        with self.assertRaises(IntegrationError) as malformed:
            self.sink.deliver(dict(payload, extra="not part of the frozen card"), str(uuid.uuid4()))
        self.assertEqual(malformed.exception.code, "invalid_prepared_notification")
        self.sink.transport = _Transport(result={"message": {}, "deduplicated": "false"})
        with self.assertRaises(IntegrationError) as receipt:
            self.sink.deliver(payload, str(uuid.uuid4()))
        self.assertEqual(receipt.exception.code, "invalid_receipt")


class HubMessagingHTTPTests(unittest.TestCase):
    def setUp(self):
        self.state = _ImServerState()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ImHandler)
        self.server.daemon_threads = True
        self.server.state = self.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.token_file = Path(self.tempdir.name) / "im-token"
        self._write_token("token-one")
        self.sink = MessagingSink({
            "name": "tasks",
            "tenant_id": "tenant-acme",
            "base_url": f"http://127.0.0.1:{self.server.server_port}",
            "installation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "token_file": str(self.token_file),
            "recipient_subject_map": {"hub-subject": "im-pairwise-subject"},
        })

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def _write_token(self, value):
        self.token_file.write_text(value + "\n", encoding="utf-8")
        self.token_file.chmod(0o600)

    def _payload(self):
        return self.sink.prepare(HubMessagingTests._fact())

    def test_pending_409_retries_same_delivery_after_private_token_rotation(self):
        self.state.responses = [
            (409, {"code": "integration_request_pending", "msg": "pending"}),
            (201, {"message": {"id": "message-123"}, "deduplicated": True}),
        ]
        payload = self._payload()
        delivery_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        with self.assertRaises(IntegrationError) as pending:
            self.sink.deliver(payload, delivery_id)
        self.assertEqual(pending.exception.status, 409)
        self.assertTrue(pending.exception.retryable)

        self._write_token("token-two")
        receipt = self.sink.deliver(payload, delivery_id)
        self.assertEqual(receipt, {"message_id": "message-123", "deduplicated": True})
        self.assertEqual(len(self.state.requests), 2)
        self.assertEqual([item["authorization"] for item in self.state.requests],
                         ["Bearer token-one", "Bearer token-two"])
        self.assertEqual([item["idempotency_key"] for item in self.state.requests],
                         [delivery_id, delivery_id])
        self.assertEqual(self.state.requests[0]["body"], self.state.requests[1]["body"])

    def test_ordinary_idempotency_conflict_is_permanent(self):
        self.state.responses = [(409, {"code": "conflict", "msg": "different request"})]
        with self.assertRaises(IntegrationError) as conflict:
            self.sink.deliver(self._payload(), str(uuid.uuid4()))
        self.assertEqual(conflict.exception.status, 409)
        self.assertFalse(conflict.exception.retryable)
        self.assertEqual(len(self.state.requests), 1)


if __name__ == "__main__":
    unittest.main()
