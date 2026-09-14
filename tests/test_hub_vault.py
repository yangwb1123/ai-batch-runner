"""HTTP contract tests for the bounded Aero Vault evidence adapter."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from pbatch.hub_vault import (
    VaultArchiveError,
    VaultArchiveSettings,
    VaultEvidenceArchive,
)
from pbatch.hub_vault_workspace import (
    MAX_WORKSPACE_JSON_BYTES,
    VaultWorkspaceArchive,
)
from pbatch.workspace_bundle import bundle_bytes


class _VaultState:
    def __init__(self):
        self.objects = {}
        self.requests = []
        self.versioning = True
        self.drop_put_ack = False
        self.redirect_config = False
        self.oversize_config = False
        self.wrong_version_header = False
        self.rotate_token_file = ""
        self.put_count = 0


class _VaultHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    @property
    def state(self):
        return self.server.state

    def do_GET(self):
        self.state.requests.append(("GET", self.path, self.headers.get("Authorization"),
                                    self.headers.get("X-Aero-Tenant")))
        if self.path == "/v1/buckets/default/config":
            if self.state.redirect_config:
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.state.oversize_config:
                body = b"{" + b" " * (3 * 1024 * 1024) + b"}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.state.rotate_token_file:
                path = Path(self.state.rotate_token_file)
                path.write_text("token-two\n", encoding="utf-8")
                path.chmod(0o600)
                self.state.rotate_token_file = ""
            self._json(200, {"name": "default", "versioning": self.state.versioning})
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/redirected":
            self._json(200, {"unexpected": True})
            return
        key = self._key(parsed.path)
        if key is None:
            self._json(404, {"error": "not found"})
            return
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith("/versions"):
            versions = list(reversed(self.state.objects.get(key, [])))
            self._json(200, {"versions": [self._version(item) for item in versions]})
            return
        version_id = query.get("version", [""])[0]
        item = next((row for row in self.state.objects.get(key, [])
                     if row["version_id"] == version_id), None)
        if item is None:
            self._json(404, {"error": "version not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        version_header = "wrong-version" if self.state.wrong_version_header else item["version_id"]
        self.send_header("X-Version-Id", version_header)
        self.send_header("ETag", f'"{item["etag"]}"')
        self.send_header("Content-Length", str(len(item["body"])))
        self.end_headers()
        self.wfile.write(item["body"])

    def do_PUT(self):
        self.state.requests.append(("PUT", self.path, self.headers.get("Authorization"),
                                    self.headers.get("X-Aero-Tenant")))
        key = self._key(urllib.parse.urlsplit(self.path).path)
        if key is None or not self.headers.get("Idempotency-Key"):
            self._json(400, {"error": "invalid request"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.state.put_count += 1
        rows = self.state.objects.setdefault(key, [])
        version_id = f"v{len(rows) + 1}"
        rows.append({"version_id": version_id, "body": body,
                     "etag": f"etag-{version_id}"})
        if self.state.drop_put_ack:
            self.state.drop_put_ack = False
            self.close_connection = True
            self.connection.close()
            return
        self._json(201, {"key": key, "size": len(body), "etag": f"etag-{version_id}"})

    def _key(self, path):
        prefix = "/v1/files/"
        if not path.startswith(prefix):
            return None
        key = urllib.parse.unquote(path[len(prefix):])
        if key.endswith("/versions"):
            key = key[:-len("/versions")]
        return key

    @staticmethod
    def _version(item):
        return {"version_id": item["version_id"], "size": len(item["body"]),
                "etag": item["etag"], "updated_at": "2026-09-12T00:00:00Z"}

    def _json(self, status, value):
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HubVaultArchiveTests(unittest.TestCase):
    def setUp(self):
        self.state = _VaultState()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _VaultHandler)
        self.server.daemon_threads = True
        self.server.state = self.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.token_file = Path(self.tempdir.name) / "vault-token"
        self.token_file.write_text("token-one\n", encoding="utf-8")
        self.token_file.chmod(0o600)
        self.archive = self._archive()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def _archive(self, **overrides):
        values = {"base_url": f"http://127.0.0.1:{self.server.server_port}",
                  "tenant_id": "tenant-a", "token_file": str(self.token_file),
                  "prefix": "hub/evidence"}
        values.update(overrides)
        return VaultEvidenceArchive(VaultArchiveSettings(**values))

    def test_archive_uploads_then_verifies_pinned_version(self):
        ref = self.archive.archive("turn-123", {"result": "passed", "stdout": "ok"})
        self.assertEqual(ref["backend"], "aero-vault")
        self.assertEqual(ref["bucket"], "default")
        self.assertEqual(ref["version_id"], "v1")
        self.assertEqual(set(ref), {"backend", "bucket", "key", "version_id",
                                    "etag", "sha256", "size"})
        self.assertEqual(ref["sha256"], hashlib.sha256(
            self.state.objects[ref["key"]][0]["body"]).hexdigest())
        self.assertEqual(ref["size"], len(self.state.objects[ref["key"]][0]["body"]))
        self.assertTrue(any(method == "GET" and "version=v1" in path
                            for method, path, *_ in self.state.requests))
        self.assertTrue(all(tenant == "tenant-a" for *_, tenant in self.state.requests))
        self.assertTrue(all(auth == "Bearer token-one"
                            for _, _, auth, _ in self.state.requests))

    def test_repeat_and_lost_ack_recover_without_duplicate_put(self):
        self.state.drop_put_ack = True
        first = self.archive.archive("turn-lost", {"result": "passed"})
        second = self.archive.archive("turn-lost", {"result": "passed"})
        self.assertEqual(first, second)
        self.assertEqual(self.state.put_count, 1)

    def test_conflicting_existing_content_fails_without_overwrite(self):
        ref = self.archive.archive("turn-conflict", {"result": "passed"})
        self.state.objects[ref["key"]][0]["body"] = b'{"different":true}'
        with self.assertRaises(VaultArchiveError) as raised:
            self.archive.archive("turn-conflict", {"result": "passed"})
        self.assertEqual(raised.exception.code, "conflicting_content")
        self.assertEqual(self.state.put_count, 1)

    def test_wrong_version_response_header_fails_closed(self):
        self.state.wrong_version_header = True
        with self.assertRaises(VaultArchiveError) as raised:
            self.archive.archive("turn-wrong-version", {"result": "passed"})
        self.assertEqual(raised.exception.code, "conflicting_content")
        self.assertEqual(self.state.put_count, 1)

    def test_bucket_without_versioning_fails_closed(self):
        self.state.versioning = False
        with self.assertRaises(VaultArchiveError) as raised:
            self.archive.archive("turn-no-version", {"result": "passed"})
        self.assertEqual(raised.exception.code, "versioning_disabled")
        self.assertEqual(self.state.put_count, 0)

    def test_evidence_is_redacted_and_bounded_before_network(self):
        ref = self.archive.archive("turn-secret", {"api_key": "do-not-store",
                                                    "stdout": "safe"})
        stored = self.state.objects[ref["key"]][0]["body"]
        self.assertNotIn(b"do-not-store", stored)
        self.assertIn(b"[REDACTED]", stored)
        bounded = self._archive(max_evidence_bytes=128)
        requests = len(self.state.requests)
        with self.assertRaises(VaultArchiveError) as raised:
            bounded.archive("turn-large", {"text": "x" * 256})
        self.assertEqual(raised.exception.code, "evidence_too_large")
        self.assertEqual(len(self.state.requests), requests)

    def test_oversized_response_is_rejected(self):
        self.state.oversize_config = True
        with self.assertRaises(VaultArchiveError) as raised:
            self.archive.archive("turn-large-response", {"ok": True})
        self.assertEqual(raised.exception.code, "response_too_large")
        self.assertEqual(self.state.put_count, 0)

    def test_redirect_is_rejected_without_following(self):
        self.state.redirect_config = True
        with self.assertRaises(VaultArchiveError) as raised:
            self.archive.archive("turn-redirect", {"ok": True})
        self.assertEqual(raised.exception.status, 302)
        self.assertFalse(any(path == "/redirected" for _, path, *_ in self.state.requests))

    def test_token_is_private_nofollow_and_reread_for_each_request(self):
        self.state.rotate_token_file = str(self.token_file)
        self.archive.archive("turn-rotate", {"ok": True})
        tokens = [auth for _, _, auth, _ in self.state.requests]
        self.assertEqual(tokens[:2], ["Bearer token-one", "Bearer token-two"])
        self.token_file.chmod(0o644)
        with self.assertRaises(VaultArchiveError) as raised:
            self.archive.archive("turn-bad-mode", {"ok": True})
        self.assertEqual(raised.exception.code, "credential_unavailable")
        self.token_file.chmod(0o600)
        link = Path(self.tempdir.name) / "token-link"
        link.symlink_to(self.token_file)
        with self.assertRaises(VaultArchiveError) as raised:
            self._archive(token_file=str(link)).archive("turn-link", {"ok": True})
        self.assertEqual(raised.exception.code, "credential_unavailable")

    def test_http_requires_literal_loopback_and_repr_hides_token(self):
        self.assertNotIn("token-one", repr(self.archive))
        with self.assertRaises(ValueError):
            self._archive(base_url="http://vault.internal:8080")


class HubVaultWorkspaceTests(unittest.TestCase):
    def _archive(self, **overrides):
        return HubVaultArchiveTests._archive(self, **overrides)

    def setUp(self):
        HubVaultArchiveTests.setUp(self)
        self.addCleanup(lambda: HubVaultArchiveTests.tearDown(self))
        self.workspace = VaultWorkspaceArchive(self.archive)

    def test_workspace_put_read_round_trip_pins_raw_bundle_version(self):
        bundle = _workspace_bundle(b"token-shaped project bytes")
        reference = self.workspace.put("session-a", bundle)
        key = "hub/evidence/workspaces/session-a/" + reference["sha256"] + ".json"
        stored = self.state.objects[key][0]["body"]
        self.assertEqual(reference["key"], key)
        self.assertEqual(stored, bundle_bytes(bundle))
        self.assertEqual(self.workspace.read(reference), bundle)
        self.assertTrue(any("version=v1" in path for method, path, *_ in self.state.requests
                            if method == "GET"))

    def test_workspace_repeat_and_lost_put_ack_do_not_duplicate(self):
        self.state.drop_put_ack = True
        bundle = _workspace_bundle(b"same snapshot")
        first = self.workspace.put("session-b", bundle)
        second = self.workspace.put("session-b", bundle)
        self.assertEqual(first, second)
        self.assertEqual(self.state.put_count, 1)

    def test_workspace_read_rejects_wrong_prefix_or_changed_digest(self):
        reference = self.workspace.put("session-c", _workspace_bundle(b"snapshot"))
        for changed in (
                {**reference, "key": "other/workspaces/session-c/" + reference["sha256"] + ".json"},
                {**reference, "sha256": "0" * 64},
                {**reference, "size": reference["size"] + 1}):
            with self.subTest(reference=changed), self.assertRaises(VaultArchiveError):
                self.workspace.read(changed)

    def test_workspace_read_fails_closed_on_wrong_version_header(self):
        reference = self.workspace.put("session-d", _workspace_bundle(b"snapshot"))
        self.state.wrong_version_header = True
        with self.assertRaises(VaultArchiveError) as raised:
            self.workspace.read(reference)
        self.assertEqual(raised.exception.code, "conflicting_content")

    def test_workspace_oversize_is_rejected_before_vault_request(self):
        oversized = b"x" * (MAX_WORKSPACE_JSON_BYTES + 1)
        requests = len(self.state.requests)
        with mock.patch("pbatch.workspace_bundle.bundle_bytes", return_value=oversized), \
                mock.patch("pbatch.workspace_bundle.bundle_digest",
                           return_value=hashlib.sha256(oversized).hexdigest()), \
                self.assertRaises(VaultArchiveError) as raised:
            self.workspace.put("session-large", _workspace_bundle(b"x"))
        self.assertEqual(raised.exception.code, "bundle_too_large")
        self.assertEqual(len(self.state.requests), requests)


def _workspace_bundle(contents: bytes) -> dict:
    import base64
    return {"schema": "pbatch.workspace.v1", "files": [
        {"path": "src/main.py", "content_b64": base64.b64encode(contents).decode("ascii")}
    ]}


if __name__ == "__main__":
    unittest.main()
