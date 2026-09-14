from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from pbatch.hub_audit import AuditSink
from pbatch.hub_ecosystem_http import IntegrationError


CONFIG = {
    "name": "governance",
    "base_url": "https://audit.example.test",
    "tenant_id": "tenant-a",
    "token_file": "/run/secrets/audit-token",
    "source_system": "agent-hub",
    "event_type": "agent.lifecycle",
    "schema_id": "agent-hub.lifecycle",
    "schema_version": 1,
}


def fact(**changes):
    value = {
        "tenant_id": "tenant-a", "event_id": "hub-source-event-1", "cursor": 42,
        "kind": "turn.completed", "created_at": 1789000000.125,
        "instance_id": "dev-one", "session_id": "session-one", "turn_id": "turn-one",
        "task_id": "", "actor_subject": "user-1", "status": "completed",
        "trigger_subject": "",
    }
    value.update(changes)
    return value


class _Transport:
    created = []

    def __init__(self, base_url, token_file, **kwargs):
        self.base_url = base_url.rstrip("/")
        self.config = (base_url, token_file, kwargs)
        self.calls = []
        self.response = None
        self.created.append(self)

    def request(self, method, path, *, body=None, headers=None, expected=(200,)):
        self.calls.append((method, path, body, headers, expected))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class AuditSinkTests(unittest.TestCase):
    def setUp(self):
        _Transport.created = []
        self.patch = patch("pbatch.hub_audit.JSONTransport", _Transport)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.sink = AuditSink(dict(CONFIG))

    def test_prepare_builds_stable_redacted_audit_envelope(self):
        event = self.sink.prepare(fact())
        self.assertEqual(event, self.sink.prepare(fact()))
        self.assertEqual(event["tenant_id"], "tenant-a")
        self.assertEqual(event["source_system"], "agent-hub")
        self.assertEqual(event["event_type"], "agent.lifecycle")
        self.assertEqual(event["action"], "turn.completed")
        self.assertEqual(event["outcome"], "completed")
        self.assertEqual(event["occurred_at"], "2026-09-10T00:26:40.125000Z")
        self.assertEqual(event["idempotency_key"], event["event_id"])
        self.assertEqual(event["actor"], {"id": "user-1", "type": "user"})
        self.assertEqual(set(event["payload"]), {
            "hub_event_id", "cursor", "kind", "created_at", "instance_id",
            "session_id", "turn_id", "actor_subject", "status",
        })
        self.assertNotIn("task_id", event["payload"])
        self.assertNotIn("prompt", event["payload"])

    def test_prepare_uses_real_cancel_actor_and_rejects_unlisted_content(self):
        event = self.sink.prepare(fact(kind="turn.cancelled", trigger_subject="operator-2"))
        self.assertEqual(event["actor"]["id"], "operator-2")
        self.assertEqual(event["payload"]["actor_subject"], "user-1")
        self.assertEqual(event["payload"]["trigger_subject"], "operator-2")
        with self.assertRaises(IntegrationError) as caught:
            self.sink.prepare(fact(prompt="must never be sent"))
        self.assertEqual(caught.exception.code, "invalid_fact")

    def test_prepare_fences_tenant_and_bad_time(self):
        with self.assertRaises(IntegrationError) as caught:
            self.sink.prepare(fact(tenant_id="other-tenant"))
        self.assertEqual(caught.exception.code, "tenant_mismatch")
        with self.assertRaises(IntegrationError) as caught:
            self.sink.prepare(fact(created_at=float("nan")))
        self.assertEqual(caught.exception.code, "invalid_fact")

    def test_fingerprint_excludes_credential_location_and_contents(self):
        same_target = dict(CONFIG, token_file="/rotated/credential-path")
        other_target = dict(CONFIG, base_url="https://audit-2.example.test")
        self.assertEqual(AuditSink(same_target).fingerprint, self.sink.fingerprint)
        self.assertNotEqual(AuditSink(other_target).fingerprint, self.sink.fingerprint)
        self.assertEqual(self.sink.kind, "audit")

    def test_deliver_posts_persisted_envelope_and_returns_checked_receipt(self):
        event = self.sink.prepare(fact())
        transport = self.sink.transport
        transport.response = {"receipt": {
            "event_id": event["event_id"], "tenant_id": "tenant-a",
            "idempotency_key": event["idempotency_key"], "status": "ledgered",
            "accepted_at": "2026-09-10T00:26:40Z", "ledgered_at": "2026-09-10T00:26:40Z",
            "stream_id": "tenant-a:source:agent-hub", "sequence": 3, "hash": "a" * 64,
        }, "receipt_url": f"/api/v1/events/{event['event_id']}/receipt"}
        receipt = self.sink.deliver(event, "outbox-delivery-id")
        self.assertEqual(transport.calls, [("POST", "/api/v1/events?wait_for=ledgered",
                                             event, None, (202,))])
        self.assertEqual(receipt["status"], "ledgered")
        self.assertEqual(receipt["hash"], "a" * 64)

    def test_accepted_receipt_is_retryable_pending_not_success(self):
        event = self.sink.prepare(fact())
        self.sink.transport.response = {"receipt": {
            "event_id": event["event_id"], "tenant_id": "tenant-a",
            "idempotency_key": event["idempotency_key"], "status": "accepted",
            "accepted_at": "2026-09-10T00:26:40Z", "ledgered_at": "0001-01-01T00:00:00Z",
            "indexed_at": "0001-01-01T00:00:00Z", "archived_at": "0001-01-01T00:00:00Z",
            "stream_id": "tenant-a:source:agent-hub", "sequence": 3, "hash": "a" * 64,
        }, "receipt_url": f"/api/v1/events/{event['event_id']}/receipt"}
        with self.assertRaises(IntegrationError) as caught:
            self.sink.deliver(event, "delivery")
        self.assertEqual(caught.exception.code, "ledger_pending")
        self.assertTrue(caught.exception.retryable)

    def test_receipt_mismatch_and_unverified_hash_fail_closed(self):
        event = self.sink.prepare(fact())
        bad_receipts = [
            {"event_id": "other", "tenant_id": "tenant-a", "status": "ledgered",
             "ledgered_at": "2026-09-09T03:06:40Z", "hash": "a" * 64, "sequence": 1},
            {"event_id": event["event_id"], "tenant_id": "other", "status": "ledgered",
             "ledgered_at": "2026-09-09T03:06:40Z", "hash": "a" * 64, "sequence": 1},
            {"event_id": event["event_id"], "tenant_id": "tenant-a", "status": "indexed",
             "ledgered_at": "2026-09-09T03:06:40Z", "hash": "invalid", "sequence": 1},
            {"event_id": event["event_id"], "tenant_id": "tenant-a", "status": "ledgered",
             "ledgered_at": "0001-01-01T00:00:00Z", "hash": "a" * 64, "sequence": 1},
            {"event_id": event["event_id"], "tenant_id": "tenant-a", "status": "ledgered",
             "ledgered_at": "2026-09-09T03:06:40Z", "sequence": 1},
        ]
        for receipt in bad_receipts:
            self.sink.transport.response = {"receipt": receipt}
            with self.subTest(receipt=receipt), self.assertRaises(IntegrationError) as caught:
                self.sink.deliver(event, "delivery")
            self.assertEqual(caught.exception.code, "receipt_mismatch")
            self.assertFalse(caught.exception.retryable)

    def test_deliver_revalidates_saved_payload_before_network(self):
        event = self.sink.prepare(fact())
        altered = copy.deepcopy(event)
        altered["payload"]["prompt"] = "private content"
        with self.assertRaises(IntegrationError) as caught:
            self.sink.deliver(altered, "delivery")
        self.assertEqual(caught.exception.code, "invalid_prepared_event")
        self.assertEqual(self.sink.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
