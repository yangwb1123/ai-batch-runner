"""Machine renewal through real HTTP, signed JWT, SQLite and managed sessions.

The local OAuth issuer and model runtime are deterministic test substitutes;
these tests do not claim a deployed Snaplink issuer or a paid agent execution.
JWT signature/claim verification, Hub persistence, Gateway receipts and the
client-credentials transport use production code.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pbatch.agent_session import AgentSession, AgentSessionOptions, create_agent_session
from pbatch.hub_auth import GATEWAY_SCOPE, USER_SCOPES, HubAuth
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_gateway import HubGateway, HubGatewayConfig
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_jwks import SnaplinkVerifier
from pbatch.hub_oauth import MachineTokenProvider
from pbatch.hub_store import HubStore
from test_hub_integration import _Runtime, _until


class _Issuer:
    """A credential-checking, locally signed OAuth token endpoint fixture."""

    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.secrets = {name: "first-secret-" + name for name in ("alpha", "beta")}
        self.issued = {name: [] for name in self.secrets}
        self.rejected = 0
        self.lock = threading.Lock()

    def token(self, name, scopes, serial=0):
        now = int(time.time())
        claims = {"iss": "https://identity.test", "aud": "agent-hub",
                  "sub": name, "tenant_id": "team", "client_id": name,
                  "iat": now, "exp": now + 60, "scope": " ".join(sorted(scopes)),
                  "jti": f"{name}-{serial}"}
        return jwt.encode(claims, self.key, algorithm="EdDSA", headers={"kid": "local"})

    def exchange(self, authorization, form):
        credentials = base64.b64decode(authorization.removeprefix("Basic "))
        name, secret = credentials.decode("utf-8").split(":", 1)
        with self.lock:
            if not authorization.startswith("Basic ") or self.secrets.get(name) != secret:
                self.rejected += 1
                return 401, {"error": "invalid_client"}
            expected = {"grant_type": ["client_credentials"], "resource": ["agent-hub"],
                        "scope": [GATEWAY_SCOPE]}
            if form != expected:
                return 400, {"error": "invalid_request"}
            token = self.token(name, [GATEWAY_SCOPE], len(self.issued[name]) + 1)
            self.issued[name].append(token)
        return 200, {"access_token": token, "token_type": "Bearer",
                     "expires_in": 10, "scope": GATEWAY_SCOPE, "token_strategy": "jwt"}

    def auth(self):
        jwk = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="local", alg="EdDSA", use="sig")
        grants = [{"tenant_id": "team", "subject": name, "client_id": name,
                   "kind": "gateway", "instances": [name], "scopes": [GATEWAY_SCOPE]}
                  for name in self.secrets]
        grants.append({"tenant_id": "team", "subject": "reader", "client_id": "reader",
                       "kind": "user", "instances": list(self.secrets),
                       "scopes": sorted(USER_SCOPES)})
        settings = {"mode": "jwt", "issuer": "https://identity.test",
                    "jwks_url": "https://identity.test/jwks", "audience": "agent-hub",
                    "grants": grants}
        verifier = SnaplinkVerifier(settings, fetcher=lambda: {"keys": [jwk]})
        return HubAuth(settings, verifier=verifier)


class _TokenHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        form = parse_qs(raw.decode("utf-8"), strict_parsing=True)
        if self.path != "/token" or self.headers.get_content_type() != (
                "application/x-www-form-urlencoded"):
            status, result = 400, {"error": "invalid_request"}
        else:
            status, result = self.server.issuer.exchange(self.headers["Authorization"], form)
        body = json.dumps(result).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _private(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


class _Fleet:
    def __init__(self, directory, issuer, hub, token_url, clock):
        self.directory, self.issuer, self.hub = directory, issuer, hub
        self.origin = f"http://127.0.0.1:{hub.server_port}"
        self.token_url, self.clock = token_url, clock
        self.user = HubClient(self.origin, issuer.token("reader", USER_SCOPES))
        self.gateways, self.providers, self.paths = [], {}, {}

    def config(self, name):
        secret = _private(self.directory / f"{name}.secret", self.issuer.secrets[name])
        values = {"hub_url": self.origin, "token_endpoint": self.token_url + "/token",
                  "client_id": name, "client_secret_file": str(secret),
                  "resource": "agent-hub", "scopes": [GATEWAY_SCOPE]}
        path = _private(self.directory / f"{name}.json", json.dumps(values))
        self.paths[name] = secret
        return path

    def create(self, name, *, hold=False):
        provider = MachineTokenProvider.from_file(str(self.config(name)), hub_url=self.origin)
        provider._clock = lambda: self.clock[0]
        self.providers[name] = provider
        runtime = _Runtime(hold)
        options = HubGatewayConfig(self.origin, cwd=str(self.directory), instance_id=name,
                                   state_dir=str(self.directory / name), poll_interval=0.1)
        gateway = HubGateway(options, client=HubClient(self.origin, token_provider=provider),
                             session_factory=lambda opts: AgentSession(
                                 runtime, replace(opts, session_id="same-local-id")))
        self.gateways.append(gateway)
        gateway.start()
        session = self.user.list_sessions(name)[0]["session_id"]
        return gateway, runtime, session

    def renew(self, *gateways):
        self.clock[0] += 10
        for gateway in gateways:
            gateway._last_heartbeat = gateway._last_poll = 0
            gateway.run_once()

    def close(self):
        for gateway in self.gateways:
            gateway.close()


@pytest.fixture
def machines(tmp_path):
    issuer = _Issuer()
    clock = [1000.0]
    token_server = ThreadingHTTPServer(("127.0.0.1", 0), _TokenHandler)
    token_server.issuer = issuer
    store = HubStore(tmp_path / "hub.sqlite3", clock=lambda: clock[0])
    hub = HubHTTPServer(("127.0.0.1", 0), store, issuer.auth())
    try:
        with _serving(token_server) as token_url, _serving(hub):
            fleet = _Fleet(tmp_path, issuer, hub, token_url, clock)
            try:
                yield fleet
            finally:
                fleet.close()
    finally:
        store.close()


@pytest.mark.parametrize("explicit", [False, True])
def test_oauth_client_construction_is_offline_and_first_call_authenticates(machines, explicit):
    path = str(machines.config("alpha"))
    options = {"oauth_config": path} if explicit else {}
    env = {} if explicit else {"PBATCH_HUB_OAUTH_CONFIG": path}
    client = HubClient.from_env(machines.origin, environ=env, **options)
    assert machines.issuer.issued["alpha"] == []
    result = client.register_instance(instance_id="alpha", boot_id="boot-alpha",
                                      name="alpha", project_id="integration")
    assert result["generation"] == 1
    assert len(machines.issuer.issued["alpha"]) == 1


def test_independent_machines_renew_without_replacing_sessions_or_replaying_prompts(machines):
    alpha, aruntime, asession = machines.create("alpha")
    beta, bruntime, bsession = machines.create("beta")
    generations = alpha.generation, beta.generation
    assert asession != bsession
    first = machines.user.send_prompt(asession, "alpha task", "alpha-request")
    second = machines.user.send_prompt(bsession, "beta task", "beta-request")
    turns = first["turn_id"], second["turn_id"]
    _until([alpha, beta], lambda: all(
        machines.user.get_turn(turn)["state"] == "completed" for turn in turns))
    machines.renew(alpha, beta)
    assert (alpha.generation, beta.generation) == generations
    assert machines.user.list_sessions("alpha")[0]["session_id"] == asession
    assert machines.user.list_sessions("beta")[0]["session_id"] == bsession
    repeated = machines.user.send_prompt(asession, "alpha task", "alpha-request")
    assert repeated["turn_id"] == first["turn_id"]
    assert repeated["state"] == "completed"
    with pytest.raises(HubError) as denied:
        alpha.client.heartbeat(instance_id="beta", generation=beta.generation)
    assert denied.value.status == 403
    assert len(machines.issuer.issued["alpha"]) == len(machines.issuer.issued["beta"]) == 2
    assert set(machines.issuer.issued["alpha"]).isdisjoint(machines.issuer.issued["beta"])
    assert aruntime.prompts == ["alpha task"]
    assert bruntime.prompts == ["beta task"]


def _reject_first_running_ack(hub, monkeypatch):
    dispatch = hub.service.dispatch
    posted = []

    def lose_ack(method, path, query, body, principal, key):
        status, result = dispatch(method, path, query, body, principal, key)
        if path.endswith("/gateway/events") and body.get("kind") == "turn.running":
            posted.append((body["event_id"], principal.subject))
            if len(posted) == 1:
                return 401, {"error": {"code": "unauthorized", "message": "fixture lost ack"}}
        return status, result

    monkeypatch.setattr(hub.service, "dispatch", lose_ack)
    return posted


def test_processed_write_401_is_not_replayed_until_gateway_retries_its_receipt(machines,
                                                                           monkeypatch):
    gateway, runtime, session = machines.create("alpha")
    posted = _reject_first_running_ack(machines.hub, monkeypatch)
    turn = machines.user.send_prompt(session, "execute once", "lost-ack")["turn_id"]
    gateway.run_once()
    assert machines.user.get_turn(turn)["state"] == "running"
    assert len(posted) == 1
    assert runtime.prompts == []
    assert len(machines.issuer.issued["alpha"]) == 1
    gateway.run_once()
    assert len(posted) == 1
    assert len(machines.issuer.issued["alpha"]) == 1
    machines.clock[0] += 5
    _until([gateway], lambda: machines.user.get_turn(turn)["state"] == "completed")
    assert len(posted) == 2
    assert posted[0] == posted[1]
    assert len(machines.issuer.issued["alpha"]) == 2
    assert runtime.prompts == ["execute once"]
    assert [event["kind"] for event in machines.user.session_events(session)] == [
        "turn.queued", "turn.running", "message.completed", "turn.completed"]
    assert gateway.generation == 1
    assert machines.user.list_sessions("alpha")[0]["session_id"] == session


def test_atomic_secret_rotation_preserves_heartbeat_and_live_cancel(machines):
    gateway, runtime, session = machines.create("alpha", hold=True)
    turn = machines.user.send_prompt(session, "long task", "cancel-after-renew")["turn_id"]
    _until([gateway], lambda: bool(runtime.prompts))
    replacement = _private(machines.directory / "replacement.secret", "rotated:secret-α")
    os.replace(replacement, machines.paths["alpha"])
    machines.issuer.secrets["alpha"] = "rotated:secret-α"
    before = machines.user.list_instances()[0]["last_seen"]
    machines.renew(gateway)
    instance = machines.user.list_instances()[0]
    assert instance["last_seen"] > before
    assert instance["generation"] == gateway.generation == 1
    assert len(machines.issuer.issued["alpha"]) == 2
    assert machines.user.cancel_turn(turn)["state"] == "cancel_requested"
    _until([gateway], lambda: machines.user.get_turn(turn)["state"] == "interrupted")
    assert runtime.prompts == ["long task"]
    assert machines.user.session_events(session)[-1]["kind"] == "turn.interrupted"
    assert machines.user.list_sessions("alpha")[0]["session_id"] == session
    assert machines.user.get_session(session)["active_turn_id"] == ""


def test_rejected_renewal_keeps_prompt_queued_until_private_secret_is_repaired(machines):
    gateway, runtime, session = machines.create("alpha")
    turn = machines.user.send_prompt(session, "after repair", "repair-secret")["turn_id"]
    machines.issuer.secrets["alpha"] = "new-server-secret"
    machines.renew(gateway)
    assert machines.issuer.rejected == 1
    assert machines.user.get_turn(turn)["state"] == "queued"
    assert runtime.prompts == []
    assert not gateway._stop.is_set()
    replacement = _private(machines.directory / "replacement.secret", "new-server-secret")
    os.replace(replacement, machines.paths["alpha"])
    machines.clock[0] += 31
    gateway._last_heartbeat = gateway._last_poll = 0
    _until([gateway], lambda: machines.user.get_turn(turn)["state"] == "completed")
    assert len(machines.issuer.issued["alpha"]) == 2
    assert runtime.prompts == ["after repair"]
    assert gateway.generation == 1
    assert machines.user.list_sessions("alpha")[0]["session_id"] == session


def _environment_reporter(directory):
    path = directory / "fake_provider.py"
    path.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "report = {'hub': {k: v for k, v in os.environ.items() "
        "if k.startswith('PBATCH_HUB_')}, "
        "'retained': os.environ.get('PBATCH_TEST_PROVIDER_VALUE')}\n"
        "Path('child-env.json').write_text(json.dumps(report))\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'local-provider'}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'id': 'answer', 'type': 'agent_message', 'text': 'local response'}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}))\n",
        encoding="utf-8")
    return shlex.join([sys.executable, str(path)])


def test_machine_configuration_is_absent_from_real_agent_session_subprocess(machines,
                                                                          monkeypatch):
    config_path = str(machines.config("alpha"))
    monkeypatch.setenv("PBATCH_HUB_OAUTH_CONFIG", config_path)
    monkeypatch.setenv("PBATCH_HUB_URL", machines.origin)
    monkeypatch.setenv("PBATCH_HUB_TEST_SECRET", "must-stay-in-controller")
    monkeypatch.setenv("PBATCH_TEST_PROVIDER_VALUE", "provider-visible")
    client = HubClient.from_env(environ={"PBATCH_HUB_URL": machines.origin,
                                       "PBATCH_HUB_OAUTH_CONFIG": config_path})
    options = HubGatewayConfig(machines.origin, cwd=str(machines.directory),
                               instance_id="alpha", state_dir=str(machines.directory / "alpha"),
                               poll_interval=0.1)
    session_options = AgentSessionOptions(cwd=str(machines.directory), runtime="codex",
                                          agent=_environment_reporter(machines.directory),
                                          memory_mode="off", timeout=5)
    gateway = HubGateway(options, client=client,
                         session_factory=lambda _opts: create_agent_session(session_options))
    machines.gateways.append(gateway)
    gateway.start()
    session = machines.user.list_sessions("alpha")[0]["session_id"]
    turn = machines.user.send_prompt(session, "check environment", "env-check")["turn_id"]
    _until([gateway], lambda: machines.user.get_turn(turn)["state"] == "completed")
    report = json.loads((machines.directory / "child-env.json").read_text())
    assert report == {"hub": {}, "retained": "provider-visible"}
    assert os.environ["PBATCH_HUB_OAUTH_CONFIG"] == config_path
    assert machines.user.session_events(session)[-2]["payload"]["text"] == "local response"
