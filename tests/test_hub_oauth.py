"""Real local OAuth exchanges exercise credential boundaries and renewal."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import shutil
import ssl
import subprocess
from pathlib import Path
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from pbatch.hub_client import HubError
from pbatch.hub_oauth import MachineTokenProvider
from pbatch.hub_oauth_config import MAX_CONFIG_BYTES, MAX_SECRET_BYTES
from pbatch.hub_oauth_http import MAX_RESPONSE_BYTES


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Endpoint:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.headers = {}
        self.content_length = True
        self.byte_delay = 0.0
        self.body = {"access_token": "initial-token", "token_type": "Bearer", "expires_in": 100}
        self.on_request = lambda: None
        self.clock = Clock()

    def respond(self, handler):
        self.calls.append({"path": handler.path, "headers": dict(handler.headers),
                           "body": handler.rfile.read(int(handler.headers["Content-Length"]))})
        self.on_request()
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        handler.send_response(self.status)
        for key, value in self.headers.items():
            handler.send_header(key, value)
        handler.send_header("Content-Type", "application/json")
        if self.content_length:
            handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        try:
            if self.byte_delay:
                for byte in raw:
                    handler.wfile.write(bytes([byte]))
                    handler.wfile.flush()
                    time.sleep(self.byte_delay)
            else:
                handler.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def config(self, directory, **updates):
        self.secret = directory / "secret"
        self.secret.write_text("initial-secret")
        self.secret.chmod(0o600)
        value = {"hub_url": self.origin, "token_endpoint": self.origin + "/identity/token",
                 "client_id": "agent:invalid", "client_secret_file": "secret",
                 "resource": "agent-hub", "scopes": ["agent:read", "agent:gateway"]}
        value.update(client_id="gateway+%客户端")
        value.update(updates)
        self.path = directory / "machine.json"
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)
        return self.path

    def provider(self, directory, **updates):
        provider = MachineTokenProvider.from_file(self.config(directory, **updates), hub_url=self.origin)
        provider._clock = self.clock
        return provider


@contextmanager
def local_endpoint(tls_context=None):
    endpoint = Endpoint()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            endpoint.respond(self)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        scheme = "https" if tls_context else "http"
        if tls_context:
            server.socket = tls_context.wrap_socket(server.socket, server_side=True)
        endpoint.origin = f"{scheme}://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            yield endpoint
        finally:
            server.shutdown()
            thread.join(2)


@pytest.fixture
def endpoint():
    with local_endpoint() as value:
        yield value


def test_lazy_exchange_uses_snaplink_basic_and_form_contract(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    assert endpoint.calls == []
    assert provider() == provider() == "initial-token"
    assert len(endpoint.calls) == 1
    call = endpoint.calls[0]
    assert call["path"] == "/identity/token"
    basic = base64.b64decode(call["headers"]["Authorization"].split(" ")[1]).decode()
    assert basic == "gateway+%客户端:initial-secret"
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert parse_qs(call["body"].decode()) == {
        "grant_type": ["client_credentials"], "resource": ["agent-hub"],
        "scope": ["agent:read agent:gateway"],
    }
    assert not any("tenant" in key.lower() for key in call["headers"])
    assert "initial-secret" not in repr(provider) and "initial-token" not in repr(provider)
    assert endpoint.origin not in repr(provider) and "gateway" not in repr(provider)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["machine.json", "secret"]


def test_concurrent_pollers_share_initial_and_renewed_token(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    with ThreadPoolExecutor(max_workers=16) as executor:
        assert list(executor.map(lambda _: provider(), range(32))) == ["initial-token"] * 32
        assert len(endpoint.calls) == 1
        endpoint.clock.now = 90
        endpoint.body["access_token"] = "renewed-token"
        assert list(executor.map(lambda _: provider(), range(32))) == ["renewed-token"] * 32
    assert len(endpoint.calls) == 2


def test_renewal_rereads_atomically_rotated_secret_and_stale_401_keeps_new_token(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    assert provider() == "initial-token"
    rotated = tmp_path / "next-secret"
    rotated.write_text("rotated:+%秘密")
    rotated.chmod(0o400)
    rotated.replace(endpoint.secret)
    endpoint.body["access_token"] = "new-token"
    endpoint.clock.now = 90
    assert provider() == "new-token"
    basic = base64.b64decode(endpoint.calls[-1]["headers"]["Authorization"].split()[1]).decode()
    assert basic == "gateway+%客户端:rotated:+%秘密"
    provider.invalidate("initial-token")
    assert provider() == "new-token" and len(endpoint.calls) == 2
    provider.invalidate("new-token")
    endpoint.body["access_token"] = "after-401"
    endpoint.clock.now += 5
    assert provider() == "after-401" and len(endpoint.calls) == 3


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_backoff_uses_only_unexpired_token(endpoint, tmp_path, status):
    provider = endpoint.provider(tmp_path)
    assert provider() == "initial-token"
    endpoint.status, endpoint.body = status, {"secret-error": "must-not-leak"}
    endpoint.clock.now = 90
    assert provider() == "initial-token"
    for _ in range(20):
        assert provider() == "initial-token"
    assert len(endpoint.calls) == 2
    for now in [91, 93, 97]:
        endpoint.clock.now = now
        assert provider() == "initial-token"
    endpoint.clock.now = 100
    with pytest.raises(HubError) as caught:
        provider()
    assert caught.value.code == "machine_auth_unavailable"
    assert len(endpoint.calls) == 5
    endpoint.status = 200
    endpoint.body = {"access_token": "recovered", "token_type": "bearer", "expires_in": 100}
    endpoint.clock.now = 105
    assert provider() == "recovered" and len(endpoint.calls) == 6


def test_outage_backoff_is_bounded_and_reset_after_success(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    endpoint.status = 503
    now = 0
    for wait in [1, 2, 4, 8, 16, 30, 30]:
        endpoint.clock.now = now
        with pytest.raises(HubError):
            provider()
        calls = len(endpoint.calls)
        endpoint.clock.now = now + wait - 0.01
        with pytest.raises(HubError):
            provider()
        assert len(endpoint.calls) == calls
        now += wait
    endpoint.status = 200
    endpoint.clock.now = now
    assert provider() == "initial-token"
    endpoint.status = 503
    endpoint.clock.now += 90
    assert provider() == "initial-token"
    calls = len(endpoint.calls)
    endpoint.clock.now += 1
    assert provider() == "initial-token" and len(endpoint.calls) == calls + 1


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403])
def test_rejected_credentials_discard_cached_token_without_following_redirect(endpoint, tmp_path, status):
    provider = endpoint.provider(tmp_path)
    assert provider() == "initial-token"
    endpoint.clock.now = 90
    endpoint.status = status
    endpoint.headers["Location"] = endpoint.origin + "/credential-exfiltration"
    endpoint.body = {"error": "invalid_client", "error_description": "initial-secret initial-token"}
    with pytest.raises(HubError) as caught:
        provider()
    assert caught.value.code == "machine_auth_rejected"
    assert caught.value.status == status
    with pytest.raises(HubError):
        provider()
    assert len(endpoint.calls) == 2
    assert provider._token == ""
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert "initial-secret" not in rendered and "initial-token" not in str(caught.value)
    assert endpoint.origin not in rendered


def test_network_failure_is_redacted_and_does_not_hammer_endpoint(tmp_path):
    with local_endpoint() as endpoint:
        provider = endpoint.provider(tmp_path)
        provider._timeout = 0.1
        assert provider() == "initial-token"
    endpoint.clock.now = 90
    assert provider() == "initial-token"
    endpoint.clock.now = 100
    with pytest.raises(HubError) as caught:
        provider()
    assert caught.value.code == "machine_auth_unavailable"
    assert endpoint.origin not in str(caught.value)
    assert "Connection" not in str(caught.value)


@pytest.mark.parametrize("body", [
    {}, [], b"not json", b'{} trailing', b'{"access_token":"x","access_token":"y"}',
    b'{"expires_in":NaN}', b'{"nested":{"x":1,"x":2}}',
    {"access_token": "", "token_type": "bearer", "expires_in": 100},
    {"access_token": "contains space", "token_type": "bearer", "expires_in": 100},
    {"access_token": "contains\rnewline", "token_type": "bearer", "expires_in": 100},
    {"access_token": "\x00", "token_type": "bearer", "expires_in": 100},
    {"access_token": "秘密", "token_type": "bearer", "expires_in": 100},
    {"access_token": "x" * 16385, "token_type": "bearer", "expires_in": 100},
    {"access_token": "x", "token_type": "basic", "expires_in": 100},
    {"access_token": "x", "token_type": "bearer", "expires_in": True},
    {"access_token": "x", "token_type": "bearer", "expires_in": 1.0},
    {"access_token": "x", "token_type": "bearer", "expires_in": "100"},
    {"access_token": "x", "token_type": "bearer", "expires_in": 0},
    {"access_token": "x", "token_type": "bearer", "expires_in": -1},
    {"access_token": "x", "token_type": "bearer", "expires_in": 86401},
    {"access_token": "x", "token_type": "bearer", "expires_in": 100, "error": "invalid_client"},
])
def test_malformed_success_response_fails_closed_and_is_backed_off(endpoint, tmp_path, body):
    provider = endpoint.provider(tmp_path)
    assert provider() == "initial-token"
    endpoint.clock.now = 90
    endpoint.body = body
    with pytest.raises(HubError) as caught:
        provider()
    assert caught.value.code == "invalid_token_response"
    with pytest.raises(HubError):
        provider()
    assert len(endpoint.calls) == 2 and provider._token == ""


@pytest.mark.parametrize("scope", [None, "", "agent:read", "agent:read agent:gateway extra",
                                  "agent:read agent:read", "agent:gateway\tagent:read",
                                  " agent:gateway agent:read", ["agent:read", "agent:gateway"]])
def test_returned_scope_must_equal_requested_set(endpoint, tmp_path, scope):
    provider = endpoint.provider(tmp_path)
    endpoint.body["scope"] = scope
    with pytest.raises(HubError) as caught:
        provider()
    assert caught.value.code == "invalid_token_response"


def test_response_accepts_exact_token_bound_scope_reordering_and_mixed_case_bearer(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    endpoint.body.update(access_token="x" * 16384, token_type="bEaReR", expires_in=86400,
                         scope="agent:gateway agent:read")
    assert provider() == "x" * 16384
    endpoint.clock.now = 86339
    assert provider() == "x" * 16384
    assert len(endpoint.calls) == 1
    endpoint.clock.now = 86340
    assert provider() == "x" * 16384 and len(endpoint.calls) == 2


def test_success_extensions_are_ignored_without_storing_extra_credentials(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    endpoint.body.update(token_strategy="jwt", refresh_token="unused-refresh", id_token="unused-id")
    assert provider() == "initial-token"
    assert "unused-refresh" not in repr(provider.__dict__)
    assert "unused-id" not in repr(provider.__dict__)


@pytest.mark.parametrize("length", [MAX_RESPONSE_BYTES, MAX_RESPONSE_BYTES + 1])
def test_body_limit_is_applied_before_parsing_and_without_length_header(endpoint, tmp_path, length):
    provider = endpoint.provider(tmp_path)
    endpoint.body = b" " * length
    endpoint.content_length = False
    with pytest.raises(HubError) as caught:
        provider()
    expected = "invalid_token_response" if length == MAX_RESPONSE_BYTES else "token_response_too_large"
    assert caught.value.code == expected


def test_short_ttl_cannot_be_cached_past_request_start_expiry(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    endpoint.body["expires_in"] = 1
    endpoint.on_request = lambda: setattr(endpoint.clock, "now", 1.1)
    with pytest.raises(HubError) as caught:
        provider()
    assert caught.value.code == "invalid_token_response"
    assert provider._token == ""


def test_almost_expired_response_has_positive_refresh_interval(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    endpoint.body["expires_in"] = 1
    endpoint.on_request = lambda: setattr(endpoint.clock, "now", 0.99)
    assert provider() == "initial-token"
    for _ in range(20):
        assert provider() == "initial-token"
    assert len(endpoint.calls) == 1
    assert 0.99 < provider._refresh_at < 1


@pytest.mark.parametrize("updates", [
    {"unknown": "value"}, {"hub_url": "https://other.test"},
    {"client_id": "has:colon"}, {"client_id": "\n"}, {"client_id": " "},
    {"resource": ""}, {"resource": "contains space"}, {"resource": "x" * 2049},
    {"scopes": []}, {"scopes": "agent:read"}, {"scopes": ["same", "same"]},
    {"scopes": ["a b"]}, {"scopes": ["a\tb"]}, {"scopes": ["quote\""]},
    {"scopes": ["back\\slash"]}, {"scopes": ["unicode秘密"]},
    {"scopes": [None]}, {"scopes": ["x" * 257]}, {"scopes": [str(i) for i in range(129)]},
    {"client_secret_file": ""}, {"token_endpoint": "http://localhost/token"},
    {"token_endpoint": "http://192.0.2.1/token"}, {"token_endpoint": "file:///secret"},
    {"token_endpoint": "https://client:password@issuer.test/token"},
    {"token_endpoint": "https://@issuer.test/token"},
    {"token_endpoint": "https://issuer.test/token?secret=yes"},
    {"token_endpoint": "https://issuer.test/token#fragment"},
    {"token_endpoint": "https://issuer.test/token?"},
    {"token_endpoint": "https://issuer.test/token#"},
    {"token_endpoint": "https://issuer.test"},
    {"token_endpoint": "https://issuer.test:0/token"},
    {"token_endpoint": "https://issuer.test:65536/token"},
    {"token_endpoint": "https://issuer.test:invalid/token"},
    {"token_endpoint": "https://issuer.test/line\n/token"},
    {"token_endpoint": "https://issuer.test\\other/token"},
    {"token_endpoint": " https://issuer.test/token"},
])
def test_config_rejects_invalid_boundaries_without_network(endpoint, tmp_path, updates):
    path = endpoint.config(tmp_path, **updates)
    with pytest.raises(ValueError) as caught:
        MachineTokenProvider.from_file(path, hub_url=endpoint.origin)
    assert str(caught.value) == "machine credential configuration is invalid or unavailable"
    assert endpoint.calls == []


@pytest.mark.parametrize("body", [
    b"[]", b"null", b'{}', b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}',
    b'{"x":"\xff"}', b"{" * 2000, b" " * (MAX_CONFIG_BYTES + 1),
])
def test_config_json_is_bounded_strict_and_redacted(endpoint, tmp_path, body):
    path = endpoint.config(tmp_path)
    path.write_bytes(body)
    with pytest.raises(ValueError) as caught:
        MachineTokenProvider.from_file(path, hub_url=endpoint.origin)
    assert str(path) not in str(caught.value)
    assert caught.value.__suppress_context__
    assert endpoint.calls == []


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o700, 0o604])
@pytest.mark.parametrize("target", ["config", "secret"])
def test_config_and_secret_must_remain_private_regular_files(endpoint, tmp_path, mode, target):
    path = endpoint.config(tmp_path)
    (path if target == "config" else endpoint.secret).chmod(mode)
    with pytest.raises(ValueError):
        MachineTokenProvider.from_file(path, hub_url=endpoint.origin)
    assert endpoint.calls == []


@pytest.mark.parametrize("special", ["symlink", "fifo", "directory", "missing"])
@pytest.mark.parametrize("target", ["config", "secret"])
def test_special_files_are_rejected_promptly_without_network(endpoint, tmp_path, special, target):
    path = endpoint.config(tmp_path)
    changed = path if target == "config" else endpoint.secret
    changed.unlink()
    if special == "symlink":
        changed.symlink_to(tmp_path / "elsewhere")
    elif special == "fifo":
        os.mkfifo(changed, 0o600)
    elif special == "directory":
        changed.mkdir(mode=0o600)
    started = time.monotonic()
    with pytest.raises(ValueError):
        MachineTokenProvider.from_file(path, hub_url=endpoint.origin)
    assert time.monotonic() - started < 1
    assert endpoint.calls == []


@pytest.mark.parametrize("secret", [b"", b" ", b"line\n", b"line\r", b"line\x00", b"\xff",
                                    b"x" * (MAX_SECRET_BYTES + 1)])
def test_invalid_rotated_secret_fails_closed_before_network(endpoint, tmp_path, secret):
    provider = endpoint.provider(tmp_path)
    assert provider() == "initial-token"
    endpoint.clock.now = 90
    endpoint.secret.write_bytes(secret)
    with pytest.raises(HubError) as caught:
        provider()
    assert caught.value.code == "credential_unavailable"
    assert len(endpoint.calls) == 1 and provider._token == ""
    assert str(endpoint.secret) not in str(caught.value)
    assert caught.value.__suppress_context__


def test_valid_read_only_files_short_reads_and_canonical_origin_are_supported(endpoint, tmp_path, monkeypatch):
    path = endpoint.config(tmp_path, hub_url="https://HUB.example.test:443/")
    path.chmod(0o400)
    endpoint.secret.write_bytes(b"x" * MAX_SECRET_BYTES)
    endpoint.secret.chmod(0o400)
    original_read = os.read
    monkeypatch.setattr(os, "read", lambda descriptor, size: original_read(descriptor, min(17, size)))
    provider = MachineTokenProvider.from_file(path, hub_url="https://hub.example.test")
    assert provider() == "initial-token"
    assert len(endpoint.calls) == 1


@pytest.mark.parametrize("timeout", [True, 0, -1, 121, float("nan"), float("inf"), "5", None, 10 ** 1000])
def test_invalid_timeout_is_fixed_value_error_without_network(endpoint, tmp_path, timeout):
    path = endpoint.config(tmp_path)
    with pytest.raises(ValueError) as caught:
        MachineTokenProvider.from_file(path, hub_url=endpoint.origin, timeout=timeout)
    assert str(caught.value) == "machine credential configuration is invalid or unavailable"
    assert endpoint.calls == []


def test_environment_proxy_cannot_receive_machine_credentials(endpoint, tmp_path, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    provider = endpoint.provider(tmp_path)
    assert provider() == "initial-token" and len(endpoint.calls) == 1


def test_slow_response_cannot_hold_renewal_lock_beyond_total_body_deadline(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    provider._timeout = 0.15
    endpoint.content_length = False
    endpoint.byte_delay = 0.03
    started = time.monotonic()
    with pytest.raises(HubError) as caught:
        provider()
    elapsed = time.monotonic() - started
    assert caught.value.code == "machine_auth_unavailable"
    assert 0.1 <= elapsed < 0.8
    for _ in range(10):
        with pytest.raises(HubError):
            provider()
    assert len(endpoint.calls) == 1


def test_rotated_symlink_or_fifo_never_blocks_or_retains_the_old_token(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    assert provider() == "initial-token"
    endpoint.clock.now = 90
    endpoint.secret.unlink()
    os.mkfifo(endpoint.secret, 0o600)
    started = time.monotonic()
    with pytest.raises(HubError) as caught:
        provider()
    assert time.monotonic() - started < 1
    assert caught.value.code == "credential_unavailable" and provider._token == ""
    assert len(endpoint.calls) == 1


def test_foreign_owner_is_rejected(endpoint, tmp_path, monkeypatch):
    path = endpoint.config(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ValueError):
        MachineTokenProvider.from_file(path, hub_url=endpoint.origin)
    assert endpoint.calls == []


def test_continuous_hub_rejection_cannot_mint_tokens_on_every_poll(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    for attempt in range(3):
        endpoint.body["access_token"] = f"token-{attempt}"
        assert provider() == f"token-{attempt}"
        provider.invalidate(f"token-{attempt}")
        for _ in range(20):
            with pytest.raises(HubError) as caught:
                provider()
            assert caught.value.code == "machine_auth_rejected"
            assert caught.value.status == 401
        assert len(endpoint.calls) == attempt + 1
        endpoint.clock.now += 4.99
        with pytest.raises(HubError):
            provider()
        endpoint.clock.now += 0.01
    endpoint.body["access_token"] = "accepted-token"
    assert provider() == "accepted-token" and len(endpoint.calls) == 4
    provider.invalidate("token-2")
    assert provider() == "accepted-token" and len(endpoint.calls) == 4


def test_old_401_cannot_extend_current_rejection_cooldown(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    assert provider() == "initial-token"
    provider.invalidate("initial-token")
    endpoint.clock.now = 4.9
    provider.invalidate("initial-token")
    provider.invalidate("")
    endpoint.clock.now = 5
    endpoint.body["access_token"] = "after-cooldown"
    assert provider() == "after-cooldown" and len(endpoint.calls) == 2


def test_untrusted_tls_endpoint_never_receives_basic_credentials(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required for the TLS integration test")
    cert, key = tmp_path / "self-signed.pem", tmp_path / "server-key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    with local_endpoint(context) as endpoint:
        provider = endpoint.provider(tmp_path)
        with pytest.raises(HubError) as caught:
            provider()
        assert caught.value.code == "machine_auth_unavailable"
        assert endpoint.calls == []
        assert endpoint.origin not in str(caught.value)


def test_oversized_declared_body_is_refused_before_slow_payload_is_read(endpoint, tmp_path):
    provider = endpoint.provider(tmp_path)
    endpoint.body = b" " * (MAX_RESPONSE_BYTES + 1)
    endpoint.byte_delay = 0.03
    started = time.monotonic()
    with pytest.raises(HubError) as caught:
        provider()
    assert caught.value.code == "token_response_too_large"
    assert time.monotonic() - started < 0.5
