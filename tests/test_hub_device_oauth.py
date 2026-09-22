"""Device login uses actual local HTTP, private profiles and monotonic polling."""

from contextlib import contextmanager
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from pbatch.hub_auth import USER_SCOPES
from pbatch.hub_device_oauth import DEVICE_GRANT, DeviceAuthorization, UserAuthError
from pbatch.hub_user_auth_config import load_user_config, profile_digest


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class Endpoint:
    def __init__(self):
        self.clock, self.calls = Clock(), []
        self.status, self.headers = 200, {}
        self.start_body = None
        self.token_replies = []
        self.delay = 0
        self.content_length = True
        self.on_request = lambda: None

    def respond(self, handler):
        raw = handler.rfile.read(int(handler.headers["Content-Length"]))
        self.calls.append((handler.path, dict(handler.headers), parse_qs(raw.decode()), self.clock()))
        status, body = self.status, self.start_body
        if handler.path == "/auth/token":
            status, body = self.token_replies.pop(0)
        self.on_request()
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        if self.content_length:
            handler.send_header("Content-Length", str(len(raw)))
        for key, value in self.headers.items():
            handler.send_header(key, value)
        handler.end_headers()
        try:
            for byte in raw:
                handler.wfile.write(bytes([byte]))
                if self.delay:
                    handler.wfile.flush()
                    time.sleep(self.delay)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def profile(self, directory, **updates):
        value = {"hub_url": "https://Hub.EXAMPLE.test/", "device_authorization_endpoint": self.origin + "/auth/device/code",
                 "token_endpoint": self.origin + "/auth/token", "client_id": "cli+%客户端",
                 "resource": "agent-hub", "scopes": ["agent.sessions:read", "openid"],
                 "credential_directory": "credentials"}
        value.update(updates)
        path = directory / "user-auth.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def client(self, directory, **updates):
        value = DeviceAuthorization(load_user_config(self.profile(directory, **updates)))
        value._clock, value._sleep = self.clock, self.clock.sleep
        value._wall_clock = lambda: 1700000000 + self.clock() - 1000
        return value

    def success(self, **updates):
        body = {"access_token": "human-access", "token_type": "Bearer", "expires_in": 100,
                "scope": "openid agent.sessions:read", "token_strategy": "jwt",
                "refresh_token": "never-save-refresh", "id_token": "never-save-id"}
        body.update(updates)
        return 200, body


@contextmanager
def local_endpoint():
    endpoint = Endpoint()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            endpoint.respond(self)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        endpoint.origin = f"http://127.0.0.1:{server.server_port}"
        endpoint.start_body = {
            "device_code": "private-device-code", "user_code": "ABCD-EFGH",
            "verification_uri": endpoint.origin + "/device/verify",
            "verification_uri_complete": endpoint.origin + "/device/verify?user_code=ABCD-EFGH",
            "expires_in": 60, "interval": 2,
        }
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


def test_public_device_contract_pending_slowdown_and_short_lived_credential(endpoint, tmp_path):
    auth = endpoint.client(tmp_path)
    endpoint.token_replies = [(400, {"error": "authorization_pending"}),
                              (400, {"error": "slow_down"}), endpoint.success()]
    challenge = auth.start()
    assert "private-device-code" not in repr(auth)
    result = auth.poll(challenge)
    assert result == {"access_token": "human-access", "issued_at": 1700000011.0, "expires_at": 1700000111.0}
    assert endpoint.clock.sleeps == [2, 2, 7]
    assert [call[3] for call in endpoint.calls] == [1000, 1002, 1004, 1011]
    assert endpoint.calls[0][2] == {"client_id": ["cli+%客户端"], "resource": ["agent-hub"],
                                    "scope": ["agent.sessions:read openid"]}
    assert endpoint.calls[1][2] == {"grant_type": [DEVICE_GRANT], "client_id": ["cli+%客户端"],
                                    "device_code": ["private-device-code"]}
    assert all("Authorization" not in call[1] for call in endpoint.calls)
    assert all(call[1]["Content-Type"] == "application/x-www-form-urlencoded" for call in endpoint.calls)
    assert "human-access" not in repr(auth) and "never-save" not in repr(auth.__dict__)
    assert list(tmp_path.iterdir()) == [tmp_path / "user-auth.json"]
    with pytest.raises(UserAuthError):
        auth.poll(challenge)


def test_repeated_slowdown_adds_five_seconds_each_time(endpoint, tmp_path):
    auth = endpoint.client(tmp_path)
    endpoint.token_replies = [(400, {"error": "slow_down"}), (400, {"error": "slow_down"}), endpoint.success()]
    assert auth.poll(auth.start())["access_token"] == "human-access"
    assert endpoint.clock.sleeps == [2, 7, 12]


@pytest.mark.parametrize("error,code", [
    ("access_denied", "user_auth_denied"), ("expired_token", "user_auth_expired"),
    ("invalid_grant", "user_auth_expired"), ("invalid_client", "user_auth_rejected"),
    ("attacker-private-code", "user_auth_rejected"),
])
def test_terminal_oauth_errors_never_leak_server_diagnostics(endpoint, tmp_path, error, code):
    auth = endpoint.client(tmp_path)
    endpoint.token_replies = [(400, {"error": error, "error_description": "private-device-code human-access"})]
    challenge = auth.start()
    with pytest.raises(UserAuthError) as caught:
        auth.poll(challenge)
    assert caught.value.code == code and caught.value.status == 400
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert "private-device-code" not in rendered and "human-access" not in rendered
    assert endpoint.origin not in rendered and "attacker-private-code" not in rendered
    assert len(endpoint.calls) == 2


def test_original_monotonic_deadline_stops_polling_without_reopening_device_grant(endpoint, tmp_path):
    auth = endpoint.client(tmp_path)
    endpoint.start_body["expires_in"] = 5
    endpoint.token_replies = [(400, {"error": "authorization_pending"})] * 2
    with pytest.raises(UserAuthError) as caught:
        auth.poll(auth.start())
    assert caught.value.code == "user_auth_expired"
    assert endpoint.clock.sleeps == [2, 2, 1]
    assert [call[0] for call in endpoint.calls] == ["/auth/device/code", "/auth/token", "/auth/token"]
    with pytest.raises(UserAuthError):
        auth.start()
    assert len(endpoint.calls) == 3


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_server_failure_reduces_poll_frequency(endpoint, tmp_path, status):
    auth = endpoint.client(tmp_path)
    endpoint.token_replies = [(status, {"error_description": "never leak"}), endpoint.success()]
    assert auth.poll(auth.start())["access_token"] == "human-access"
    assert endpoint.clock.sleeps == [2, 4]
    assert len(endpoint.calls) == 3


def test_network_timeouts_back_off_without_restarting_authorization(tmp_path):
    with local_endpoint() as endpoint:
        auth = endpoint.client(tmp_path)
        challenge = auth.start()
    auth._timeout = 0.05
    with pytest.raises(UserAuthError) as caught:
        auth.poll(challenge)
    assert caught.value.code == "user_auth_expired"
    assert endpoint.clock.sleeps == [2, 4, 8, 16, 30]
    assert len(endpoint.calls) == 1


def test_default_poll_interval_is_five_seconds(endpoint, tmp_path):
    del endpoint.start_body["interval"]
    auth = endpoint.client(tmp_path)
    endpoint.token_replies = [endpoint.success()]
    assert auth.poll(auth.start())["access_token"] == "human-access"
    assert endpoint.clock.sleeps == [5]


@pytest.mark.parametrize("key,value", [("device_code", "tampered"), ("expires_in", 1800), ("interval", 1)])
def test_caller_cannot_replace_or_extend_the_original_challenge(endpoint, tmp_path, key, value):
    auth = endpoint.client(tmp_path)
    challenge = auth.start()
    challenge[key] = value
    with pytest.raises(UserAuthError) as caught:
        auth.poll(challenge)
    assert caught.value.code == "invalid_user_auth_challenge" and len(endpoint.calls) == 1


def test_keyboard_interrupt_exits_poll_before_any_token_request(endpoint, tmp_path):
    auth = endpoint.client(tmp_path)
    challenge = auth.start()

    def cancel(_delay):
        raise KeyboardInterrupt

    auth._sleep = cancel
    with pytest.raises(KeyboardInterrupt):
        auth.poll(challenge)
    assert len(endpoint.calls) == 1


@pytest.mark.parametrize("updates", [
    {"device_code": ""}, {"device_code": "x" * 2049}, {"user_code": "a\x1bb"},
    {"device_code": "x\n"}, {"user_code": "x" * 129}, {"expires_in": 0},
    {"expires_in": True}, {"expires_in": 1801}, {"expires_in": "60"}, {"expires_in": 1.1},
    {"interval": 0}, {"interval": True}, {"interval": 301},
    {"verification_uri": "https://evil.example.test/verify"},
    {"verification_uri": "http://localhost/verify"},
    {"verification_uri": "https://user:secret@evil.example.test/verify"},
    {"verification_uri": "https://evil.example.test/line\n"},
    {"verification_uri_complete": "https://evil.example.test/verify"},
    {"error": "private-device-code"},
])
def test_bad_challenge_is_rejected_before_display_or_poll(endpoint, tmp_path, updates):
    auth = endpoint.client(tmp_path)
    endpoint.start_body.update(updates)
    with pytest.raises(UserAuthError) as caught:
        auth.start()
    assert caught.value.code == "invalid_user_auth_response"
    assert len(endpoint.calls) == 1 and auth._challenge is None


def test_explicit_verification_origin_can_use_a_separate_trusted_ui(endpoint, tmp_path):
    auth = endpoint.client(tmp_path, verification_origin="https://Login.EXAMPLE.test/")
    endpoint.start_body.update(verification_uri="https://login.example.test/device/verify",
                              verification_uri_complete="https://login.example.test/device/verify?user_code=ABCD-EFGH")
    challenge = auth.start()
    assert challenge["verification_uri"] == "https://login.example.test/device/verify"


@pytest.mark.parametrize("updates", [
    {"access_token": ""}, {"access_token": "x" * 16385}, {"access_token": "x\n"},
    {"access_token": "x y"}, {"access_token": "秘密"}, {"token_type": "Basic"},
    {"expires_in": True}, {"expires_in": 0}, {"expires_in": 86401}, {"expires_in": 1.0},
    {"scope": "agent.gateway:connect"}, {"scope": "agent.sessions:read openid offline_access"},
    {"scope": "agent.sessions:read"}, {"scope": ["openid"]}, {"error": "authorization_pending"},
])
def test_token_response_validation_prevents_scope_expansion_or_invalid_credentials(endpoint, tmp_path, updates):
    auth = endpoint.client(tmp_path)
    endpoint.token_replies = [endpoint.success(**updates)]
    with pytest.raises(UserAuthError) as caught:
        auth.poll(auth.start())
    assert caught.value.code == "invalid_user_auth_response" and len(endpoint.calls) == 2


def test_omitted_scope_uses_request_and_optional_oidc_tokens_are_ignored(endpoint, tmp_path):
    auth = endpoint.client(tmp_path)
    status, value = endpoint.success(token_type="bEaReR", access_token="x" * 16384)
    del value["scope"]
    endpoint.token_replies = [(status, value)]
    credential = auth.poll(auth.start())
    assert set(credential) == {"access_token", "issued_at", "expires_at"}
    assert len(credential["access_token"]) == 16384


@pytest.mark.parametrize("body", [b"[]", b"null", b"not-json", b'{"x":NaN}',
                                  b'{"x":1e999}', b'{"x":1,"x":2}', b"{" * 2000])
@pytest.mark.parametrize("where", ["start", "poll"])
def test_strict_bounded_json_applies_to_success_and_error_bodies(endpoint, tmp_path, body, where):
    auth = endpoint.client(tmp_path)
    if where == "start":
        endpoint.start_body = body
        operation = auth.start
    else:
        challenge = auth.start()
        endpoint.token_replies = [(400, body)]
        operation = lambda: auth.poll(challenge)
    with pytest.raises(UserAuthError) as caught:
        operation()
    assert caught.value.code == "invalid_user_auth_response"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_device_endpoint_redirects_are_never_followed(endpoint, tmp_path, status):
    auth = endpoint.client(tmp_path)
    endpoint.status = status
    endpoint.headers["Location"] = endpoint.origin + "/credential-exfiltration"
    with pytest.raises(UserAuthError) as caught:
        auth.start()
    assert caught.value.code == "user_auth_rejected" and len(endpoint.calls) == 1


def test_slow_body_has_a_total_read_deadline_and_no_error_payload_leaks(endpoint, tmp_path):
    auth = endpoint.client(tmp_path)
    auth._timeout = 0.1
    endpoint.content_length, endpoint.delay = False, 0.03
    started = time.monotonic()
    with pytest.raises(UserAuthError) as caught:
        auth.start()
    assert caught.value.code == "user_auth_unavailable"
    assert 0.05 < time.monotonic() - started < 0.8
    assert len(endpoint.calls) == 1


@pytest.mark.parametrize("where", ["start", "poll"])
def test_oversized_json_is_rejected_on_both_endpoints(endpoint, tmp_path, where):
    auth = endpoint.client(tmp_path)
    if where == "start":
        endpoint.start_body = b" " * 32769
        operation = auth.start
    else:
        challenge = auth.start()
        endpoint.token_replies = [(400, b" " * 32769)]
        operation = lambda: auth.poll(challenge)
    with pytest.raises(UserAuthError) as caught:
        operation()
    assert caught.value.code == "user_auth_response_too_large"


def test_environment_proxy_is_disabled(endpoint, tmp_path, monkeypatch):
    for key in ["HTTP_PROXY", "http_proxy"]:
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    for key in ["NO_PROXY", "no_proxy"]:
        monkeypatch.setenv(key, "")
    assert endpoint.client(tmp_path).start()["user_code"] == "ABCD-EFGH"


def test_profile_digest_binds_permissions_and_endpoints_but_not_cache_location(endpoint, tmp_path):
    path = endpoint.profile(tmp_path)
    config = load_user_config(path, hub_url="https://hub.example.test:443")
    digest = profile_digest(config)
    assert len(digest) == 64 and int(digest, 16)
    assert config["credential_directory"] == str(tmp_path / "credentials")
    assert config["verification_origin"] == endpoint.origin
    assert profile_digest(dict(config, credential_directory="another-cache")) == digest
    assert profile_digest(dict(config, scopes=list(reversed(config["scopes"])))) == digest
    assert profile_digest(dict(config, client_id="other")) != digest
    assert profile_digest(dict(config, token_endpoint=endpoint.origin + "/different/token")) != digest
    assert profile_digest(dict(config, verification_origin="https://ui.example.test")) != digest
    assert not (tmp_path / "credentials").exists()
    assert endpoint.calls == []


@pytest.mark.parametrize("updates", [
    {"extra": "value"}, {"scopes": []}, {"scopes": ["openid"]},
    {"scopes": ["offline_access"]}, {"scopes": ["agent.gateway:connect"]},
    {"scopes": ["agent.sessions:read", "agent.sessions:read"]}, {"scopes": [None]},
    {"scopes": "agent.sessions:read"}, {"scopes": ["email", "agent.sessions:read"]},
    {"token_endpoint": "https://different.example.test/token"},
    {"token_endpoint": "http://localhost/token"}, {"token_endpoint": "http://192.0.2.1/token"},
    {"token_endpoint": "file:///token"}, {"token_endpoint": "https://user:pass@issuer.test/token"},
    {"device_authorization_endpoint": "https://issuer.test/device?query"},
    {"verification_origin": "https://ui.example.test/with/path"},
    {"resource": ""}, {"resource": "space value"}, {"client_id": "line\n"},
    {"credential_directory": ""},
])
def test_invalid_user_profile_is_fixed_value_error_without_network(endpoint, tmp_path, updates):
    path = endpoint.profile(tmp_path, **updates)
    with pytest.raises(ValueError) as caught:
        load_user_config(path)
    assert str(caught.value) == "user authorization configuration is invalid or unavailable"
    assert endpoint.calls == []


def test_all_current_agent_user_scopes_are_allowed(endpoint, tmp_path):
    assert USER_SCOPES == {"agent.instances:read", "agent.sessions:read", "agent.sessions:write",
                           "agent.turns:cancel", "agent.devices:read", "agent.tasks:read",
                           "agent.tasks:write", "agent.tasks:cancel",
                           "agent.integrations:read", "agent.integrations:retry"}
    value = load_user_config(endpoint.profile(tmp_path, scopes=sorted(USER_SCOPES | {"openid"})))
    assert value["scopes"] == sorted(USER_SCOPES | {"openid"})


@pytest.mark.parametrize("change", ["mode", "symlink", "fifo", "directory", "missing", "oversized", "duplicate"])
def test_user_profile_must_be_owned_private_regular_bounded_json(endpoint, tmp_path, change):
    path = endpoint.profile(tmp_path)
    if change == "mode":
        path.chmod(0o644)
    elif change == "oversized":
        path.write_bytes(b" " * 32769)
    elif change == "duplicate":
        path.write_bytes(b'{"scope":1,"scope":2}')
    else:
        path.unlink()
        if change == "symlink":
            path.symlink_to(tmp_path / "elsewhere")
        elif change == "fifo":
            os.mkfifo(path, 0o600)
        elif change == "directory":
            path.mkdir(mode=0o700)
    started = time.monotonic()
    with pytest.raises(ValueError):
        load_user_config(path)
    assert time.monotonic() - started < 1 and endpoint.calls == []


@pytest.mark.parametrize("timeout", [True, 0, -1, 121, float("nan"), float("inf"), 10 ** 1000])
def test_bad_timeout_is_rejected_without_request(endpoint, tmp_path, timeout):
    config = load_user_config(endpoint.profile(tmp_path))
    with pytest.raises(ValueError):
        DeviceAuthorization(config, timeout=timeout)
    assert endpoint.calls == []


def test_socket_timeout_doubles_interval_before_the_next_poll(endpoint, tmp_path):
    auth = endpoint.client(tmp_path)
    auth._timeout = 0.05
    challenge = auth.start()
    endpoint.token_replies = [endpoint.success(), endpoint.success()]

    def delay_first_poll():
        endpoint.delay = 0.03 if len(endpoint.calls) == 2 else 0

    endpoint.on_request = delay_first_poll
    assert auth.poll(challenge)["access_token"] == "human-access"
    assert endpoint.clock.sleeps == [2, 4]
    assert len(endpoint.calls) == 3


def test_challenge_lifetime_includes_time_spent_starting_the_request(endpoint, tmp_path):
    auth = endpoint.client(tmp_path)
    endpoint.start_body["expires_in"] = 5
    endpoint.on_request = lambda: setattr(endpoint.clock, "now", 1005)
    with pytest.raises(UserAuthError) as caught:
        auth.start()
    assert caught.value.code == "user_auth_expired" and auth._challenge is None


def test_poll_cannot_accept_an_access_token_after_its_lifetime_passed(endpoint, tmp_path):
    auth = endpoint.client(tmp_path)
    challenge = auth.start()
    endpoint.token_replies = [endpoint.success(expires_in=1)]
    endpoint.on_request = lambda: setattr(endpoint.clock, "now", endpoint.clock() + 1)
    with pytest.raises(UserAuthError) as caught:
        auth.poll(challenge)
    assert caught.value.code == "invalid_user_auth_response"


def test_pending_error_cannot_smuggle_a_success_credential(endpoint, tmp_path):
    auth = endpoint.client(tmp_path)
    endpoint.token_replies = [(400, {"error": "authorization_pending", "access_token": "smuggled"})]
    with pytest.raises(UserAuthError) as caught:
        auth.poll(auth.start())
    assert caught.value.code == "invalid_user_auth_response" and len(endpoint.calls) == 2


def test_owned_read_only_profile_supported_and_mismatched_hub_rejected(endpoint, tmp_path, monkeypatch):
    path = endpoint.profile(tmp_path)
    path.chmod(0o400)
    original_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, size: original_read(fd, min(size, 17)))
    assert load_user_config(path)["client_id"] == "cli+%客户端"
    with pytest.raises(ValueError):
        load_user_config(path, "https://other-hub.example.test")
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ValueError):
        load_user_config(path)
    assert endpoint.calls == []


@pytest.mark.parametrize("where,encoded", [("user_code", False), ("verification_uri", False),
                                           ("verification_uri", True),
                                           ("verification_uri_complete", False),
                                           ("verification_uri_complete", True)])
def test_private_device_code_cannot_be_in_any_displayable_challenge_field(endpoint, tmp_path, where, encoded):
    auth = endpoint.client(tmp_path)
    private = endpoint.start_body["device_code"]
    text = "".join(f"%{ord(char):02X}" for char in private) if encoded else private
    endpoint.start_body[where] = text if where == "user_code" else endpoint.origin + "/verify?code=" + text
    with pytest.raises(UserAuthError) as caught:
        auth.start()
    assert caught.value.code == "invalid_user_auth_response"
    assert private not in str(caught.value) and endpoint.calls[0][0] == "/auth/device/code"
    assert auth._challenge is None
