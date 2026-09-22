"""Real device HTTP, signed JWT Hub, SQLite, CLI, Gateway and AgentSession.

Only the issuer/JWKS fetch and model boundary are local substitutes. Device poll
waits use a deterministic clock; HTTP, verification, persistence, cache fencing
and session/turn routing use production code without a paid model or deployment.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pbatch import hub_cli, hub_device_oauth
from pbatch.agent_session import AgentSession
from pbatch.hub_auth import GATEWAY_SCOPE, USER_SCOPES
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_device_oauth import DEVICE_GRANT, DeviceAuthorization
from pbatch.hub_gateway import HubGateway, HubGatewayConfig
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore
from pbatch.hub_user_auth_config import load_user_config
from pbatch.hub_user_credential_files import STATE_FILE
from pbatch.hub_user_credentials import CredentialStore
from test_hub_integration import _Runtime, _until
from test_hub_machine_auth_integration import _Issuer, _private


class DeviceIssuer(_Issuer):
    def __init__(self):
        super().__init__()
        self.device_codes, self.human_tokens, self.requests = [], [], []
        self.pending = {}
        self.key_clock = 1000.0
        self.block_fetch = False
        self.fetch_entered, self.fetch_release = threading.Event(), threading.Event()
        self.fetch_release.set()
        self.denied = False
        self.leak_challenge = False

    def device_request(self, path, headers, form):
        self.requests.append((path, dict(headers), form))
        if headers.get("Authorization") or headers.get_content_type() != "application/x-www-form-urlencoded":
            return 400, {"error": "invalid_request"}
        if path == "/device/code":
            return self.start_device(form)
        if path == "/token":
            return self.poll_device(form)
        return 404, {"error": "not_found"}

    def start_device(self, form):
        expected = {"client_id": ["reader"], "scope": [" ".join(sorted(USER_SCOPES))], "resource": ["agent-hub"]}
        if form != expected:
            return 400, {"error": "invalid_request"}
        code = f"private-device-grant-{len(self.device_codes) + 1}"
        self.device_codes.append(code)
        self.pending[code] = {"scope": form["scope"][0], "polls": 0}
        user_code = f"ABCD-{len(self.device_codes):04d}"
        uri = self.origin + "/device/verify"
        if self.leak_challenge:
            uri += "?device=" + "".join(f"%{ord(char):02X}" for char in code)
        return 200, {"device_code": code, "user_code": user_code, "verification_uri": uri,
                     "verification_uri_complete": self.origin + "/device/verify?user_code=" + user_code,
                     "expires_in": 600, "interval": 1}

    def poll_device(self, form):
        code = form.get("device_code", [""])[0]
        expected = {"client_id": ["reader"], "device_code": [code], "grant_type": [DEVICE_GRANT]}
        if form != expected or code not in self.pending:
            return 400, {"error": "invalid_grant"}
        if self.denied:
            return 400, {"error": "access_denied", "error_description": code + " private-human-token"}
        grant = self.pending[code]
        grant["polls"] += 1
        if grant["polls"] == 1:
            return 400, {"error": "authorization_pending"}
        token = self.token("reader", grant["scope"].split(), len(self.human_tokens) + 1)
        self.human_tokens.append(token)
        del self.pending[code]
        return 200, {"access_token": token, "expires_in": 60, "token_type": "Bearer",
                     "scope": grant["scope"], "token_strategy": "jwt",
                     "refresh_token": "never-save-refresh", "id_token": "never-save-id"}

    def fetch_keys(self):
        if self.block_fetch:
            self.fetch_entered.set()
            if not self.fetch_release.wait(5):
                raise TimeoutError("local JWKS fixture was not released")
            self.block_fetch = False
        value = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(self.key.public_key()))
        value.update(kid="local", alg="EdDSA", use="sig")
        return {"keys": [value]}

    def auth(self):
        auth = super().auth()
        auth._verifier._clock = lambda: self.key_clock
        auth._verifier._fetcher = self.fetch_keys
        return auth


class DeviceHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        form = parse_qs(raw.decode("utf-8"), strict_parsing=True)
        status, body = self.server.issuer.device_request(self.path, self.headers, form)
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@contextmanager
def serving(server):
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def fast_authorization(config, timeout=5):
    auth = DeviceAuthorization(config, timeout=timeout)
    clock = [1000.0]
    auth._clock = lambda: clock[0]
    auth._sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    return auth


class UserFleet:
    def __init__(self, directory, issuer, hub):
        self.directory, self.issuer, self.hub = directory, issuer, hub
        self.origin = f"http://127.0.0.1:{hub.server_port}"
        config = {"hub_url": self.origin, "device_authorization_endpoint": issuer.origin + "/device/code",
                  "token_endpoint": issuer.origin + "/token", "client_id": "reader", "resource": "agent-hub",
                  "scopes": sorted(USER_SCOPES), "credential_directory": "human-credentials"}
        self.path = _private(directory / "user-auth.json", json.dumps(config))
        self.store = CredentialStore(load_user_config(self.path))
        self.gateways, self.runtimes = [], {}

    def create_gateways(self):
        for name in ("alpha", "beta"):
            runtime = _Runtime()
            options = HubGatewayConfig(self.origin, cwd=str(self.directory), instance_id=name,
                                       state_dir=str(self.directory / name), poll_interval=0.1)
            client = HubClient(self.origin, self.issuer.token(name, [GATEWAY_SCOPE]))
            gateway = HubGateway(options, client=client, session_factory=lambda opts, runtime=runtime:
                                 AgentSession(runtime, replace(opts, session_id="same-local-id")))
            self.gateways.append(gateway)
            self.runtimes[name] = runtime
            gateway.start()

    def login(self):
        return hub_cli.hub_main(["auth", "login", "--config", str(self.path)])

    def client(self):
        return HubClient.from_env(environ={"PBATCH_HUB_USER_CONFIG": str(self.path)})

    def close(self):
        self.issuer.fetch_release.set()
        for gateway in self.gateways:
            gateway.close()


@pytest.fixture
def people(tmp_path, monkeypatch):
    issuer = DeviceIssuer()
    token_server = ThreadingHTTPServer(("127.0.0.1", 0), DeviceHandler)
    token_server.issuer = issuer
    store = HubStore(tmp_path / "hub.sqlite3")
    hub = HubHTTPServer(("127.0.0.1", 0), store, issuer.auth())
    monkeypatch.setattr(hub_device_oauth, "DeviceAuthorization", fast_authorization)
    try:
        with serving(token_server) as origin, serving(hub):
            issuer.origin = origin
            fleet = UserFleet(tmp_path, issuer, hub)
            monkeypatch.setenv("PBATCH_HUB_USER_CONFIG", str(fleet.path))
            for key in ("PBATCH_HUB_TOKEN", "PBATCH_HUB_TOKEN_FILE", "PBATCH_HUB_OAUTH_CONFIG", "PBATCH_HUB_URL"):
                monkeypatch.delenv(key, raising=False)
            fleet.create_gateways()
            try:
                yield fleet
            finally:
                fleet.close()
    finally:
        store.close()


def assert_no_credentials(output, issuer):
    text = output.out + output.err
    for secret in issuer.device_codes + issuer.human_tokens + ["never-save-refresh", "never-save-id", "private-human-token"]:
        assert secret not in text


def test_cli_login_lists_two_instances_executes_prompt_once_and_logout_affects_existing_client(people, capsys):
    assert people.login() == 0
    login_output = capsys.readouterr()
    assert "Open: " + people.issuer.origin + "/device/verify" in login_output.out
    assert "Enter code: ABCD-0001" in login_output.out
    assert_no_credentials(login_output, people.issuer)
    client = people.client()
    assert hub_cli.hub_main(["sessions", "--json"]) == 0
    sessions = json.loads(capsys.readouterr().out)
    assert {row["instance_id"] for row in sessions} == {"alpha", "beta"}
    assert len({row["session_id"] for row in sessions}) == 2
    session = next(row["session_id"] for row in sessions if row["instance_id"] == "alpha")
    send = ["send", session, "execute once", "--idempotency-key", "human-one"]
    assert hub_cli.hub_main(send) == 0
    turn = json.loads(capsys.readouterr().out)["turn_id"]
    _until(people.gateways, lambda: client.get_turn(turn)["state"] == "completed")
    assert hub_cli.hub_main(send) == 0
    assert json.loads(capsys.readouterr().out)["turn_id"] == turn
    assert people.runtimes["alpha"].prompts == ["execute once"]
    assert people.runtimes["beta"].prompts == []
    assert hub_cli.hub_main(["auth", "logout"]) == 0
    assert_no_credentials(capsys.readouterr(), people.issuer)
    with pytest.raises(HubError) as logged_out:
        client.list_sessions()
    assert logged_out.value.code == "user_login_required"
    assert people.login() == 0
    assert len(client.list_sessions()) == 2
    assert_no_credentials(capsys.readouterr(), people.issuer)
    assert len(people.issuer.device_codes) == len(people.issuer.human_tokens) == 2
    state = (people.directory / "human-credentials" / STATE_FILE).read_text()
    assert "never-save-refresh" not in state and "never-save-id" not in state
    assert people.issuer.device_codes[0] not in state


def test_delayed_real_401_cannot_clear_the_newly_logged_in_token(people, capsys):
    assert people.login() == 0
    client = people.client()
    assert len(client.list_sessions()) == 2
    first = people.issuer.human_tokens[-1]
    people.issuer.key = Ed25519PrivateKey.generate()
    people.issuer.key_clock += 301
    people.issuer.block_fetch = True
    people.issuer.fetch_release.clear()
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.list_sessions)
        assert people.issuer.fetch_entered.wait(2)
        try:
            assert people.login() == 0
            second = people.issuer.human_tokens[-1]
            assert first != second
        finally:
            people.issuer.fetch_release.set()
        with pytest.raises(HubError) as rejected:
            pending.result(timeout=5)
        assert rejected.value.status == 401
    assert people.store.token() == second
    assert len(client.list_sessions()) == 2
    assert_no_credentials(capsys.readouterr(), people.issuer)


def test_permission_denial_keeps_user_credential_and_does_not_execute_prompt(people, capsys):
    assert people.login() == 0
    client = people.client()
    session = client.list_sessions("alpha")[0]["session_id"]
    token = people.store.token()
    people.hub.auth.grants = [replace(grant, scopes=grant.scopes - {"agent.sessions:write"})
                              if grant.kind == "user" else grant for grant in people.hub.auth.grants]
    assert hub_cli.hub_main(["send", session, "not authorized", "--idempotency-key", "denied"]) == 2
    output = capsys.readouterr()
    assert "scope" in output.err
    assert people.store.token() == token
    assert len(client.list_sessions()) == 2 and people.runtimes["alpha"].prompts == []
    assert_no_credentials(output, people.issuer)


def test_denied_relogin_preserves_existing_session_and_hides_issuer_error_description(people, capsys):
    assert people.login() == 0
    token = people.store.token()
    people.issuer.denied = True
    assert people.login() == 2
    assert people.store.token() == token
    assert people.store.status()["pending"] is False
    assert len(people.client().list_sessions()) == 2
    output = capsys.readouterr()
    assert "authorization was denied" in output.err
    assert_no_credentials(output, people.issuer)


def test_cli_never_prints_a_verification_uri_containing_percent_encoded_device_secret(people, capsys):
    people.issuer.leak_challenge = True
    assert people.login() == 2
    output = capsys.readouterr()
    assert "Open:" not in output.out and "Enter code:" not in output.out
    assert "authorization response is invalid" in output.err
    assert_no_credentials(output, people.issuer)
    assert people.store.status() == {"authenticated": False, "pending": False}
    assert len(people.issuer.requests) == 1



def test_cancelled_real_login_clears_pending_without_printing_or_issuing_credentials(people, capsys, monkeypatch):
    def cancelled_authorization(config, timeout=5):
        auth = fast_authorization(config, timeout)

        def cancel(_seconds):
            raise KeyboardInterrupt

        auth._sleep = cancel
        return auth

    monkeypatch.setattr(hub_device_oauth, "DeviceAuthorization", cancelled_authorization)
    assert people.login() == 130
    output = capsys.readouterr()
    assert "cancelled" in output.err and "session saved" not in output.out
    assert_no_credentials(output, people.issuer)
    assert people.store.status() == {"authenticated": False, "pending": False}
    assert len(people.issuer.requests) == 1 and people.issuer.human_tokens == []
