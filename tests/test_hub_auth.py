"""Real signed Snaplink-shaped access tokens and instance-scoped authority."""

from __future__ import annotations

import hashlib
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pbatch.hub_auth import GATEWAY_SCOPE, HubAuth, HubPrincipal
from pbatch.hub_errors import HubAccessError
from pbatch.hub_jwks import SnaplinkVerifier


def grant(**overrides):
    return dict(tenant_id="team", subject="alice", client_id="console", kind="user",
                scopes=["agent.sessions:read", "agent.sessions:write"],
                instances=["alpha"], **overrides)


@pytest.fixture
def signed():
    key = Ed25519PrivateKey.generate()
    jwk = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="snaplink-k1", alg="EdDSA", use="sig")
    settings = {"mode": "jwt", "issuer": "https://identity.test",
                "jwks_url": "https://identity.test/.well-known/jwks.json",
                "audience": "agent-hub", "grants": [grant()]}
    verifier = SnaplinkVerifier(settings, fetcher=lambda: {"keys": [jwk]})
    auth = HubAuth(settings, verifier=verifier)

    def token(**overrides):
        claims = {"iss": settings["issuer"], "aud": "agent-hub", "sub": "alice",
                  "tenant_id": "team", "client_id": "console", "exp": int(time.time()) + 60,
                  "iat": int(time.time()), "scope": "agent.sessions:read agent.sessions:write"}
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": "snaplink-k1"})
    return auth, token, verifier, jwk, key


def test_signed_token_intersects_scopes_and_instance_grants(signed):
    auth, token, *_ = signed
    principal = auth.authenticate("Bearer " + token(scope="agent.sessions:read admin:write"))
    principal.require("agent.sessions:read", "alpha")
    assert principal.scopes == {"agent.sessions:read"}
    for scope, instance in [("agent.sessions:write", "alpha"), ("agent.sessions:read", "beta")]:
        with pytest.raises(HubAccessError) as error:
            principal.require(scope, instance)
        assert error.value.status == 403


@pytest.mark.parametrize("claims", [
    {"iss": "https://evil.test"}, {"aud": "aero-im"}, {"exp": 1},
    {"nbf": int(time.time()) + 3600}, {"sub": ""}, {"tenant_id": ""},
    {"client_id": ""}, {"scope": ["agent.sessions:read"]},
    {"cnf": {"jkt": "needs-proof"}}, {"act": {"sub": "delegator"}},
])
def test_invalid_or_unproved_access_tokens_rejected(signed, claims):
    auth, token, *_ = signed
    with pytest.raises(HubAccessError) as error:
        auth.authenticate("Bearer " + token(**claims))
    assert error.value.status == 401


@pytest.mark.parametrize("claims", [
    {"tenant_id": "other"}, {"sub": "bob"}, {"client_id": "other-client"},
])
def test_signed_identity_cannot_choose_another_grant(signed, claims):
    auth, token, *_ = signed
    with pytest.raises(HubAccessError) as error:
        auth.authenticate("Bearer " + token(**claims))
    assert error.value.status == 403


def test_unsigned_or_different_key_token_rejected(signed):
    auth, token, *_ = signed
    claims = jwt.decode(token(), options={"verify_signature": False})
    for value in [jwt.encode(claims, "", algorithm="none"),
                  jwt.encode(claims, "wrong-secret" * 4, algorithm="HS256", headers={"kid": "snaplink-k1"})]:
        with pytest.raises(HubAccessError) as error:
            auth.authenticate("Bearer " + value)
        assert error.value.status == 401


def test_jwks_rotation_refreshes_and_expired_cache_fails_closed(signed):
    _auth, token, verifier, jwk, _key = signed
    now = [1.0]
    verifier._clock = lambda: now[0]
    assert verifier.verify(token())["sub"] == "alice"
    next_key = Ed25519PrivateKey.generate()
    next_jwk = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(next_key.public_key()))
    next_jwk.update(kid="next-key", alg="EdDSA")
    verifier._fetcher = lambda: {"keys": [jwk, next_jwk]}
    claims = jwt.decode(token(), options={"verify_signature": False})
    rotated = jwt.encode(claims, next_key, algorithm="EdDSA", headers={"kid": "next-key"})
    now[0] += 6
    assert verifier.verify(rotated)["sub"] == "alice"
    now[0] += 301
    verifier._fetcher = lambda: {"keys": []}
    with pytest.raises(HubAccessError) as error:
        verifier.verify(token())
    assert error.value.status == 503


def test_gateway_grants_cannot_widen_to_other_instances():
    row = {"tenant_id": "team", "subject": "node", "client_id": "node", "kind": "gateway",
           "instances": ["alpha", "beta"], "scopes": [GATEWAY_SCOPE], "token_sha256": "a" * 64}
    with pytest.raises(ValueError, match="exactly one"):
        HubAuth({"mode": "development", "grants": [row]})


def test_development_tokens_require_explicit_hash_and_bearer():
    row = grant(token_sha256=hashlib.sha256(b"local-test-access").hexdigest())
    auth = HubAuth({"mode": "development", "grants": [row]})
    assert auth.authenticate("Bearer local-test-access").subject == "alice"
    for header in ["", "Basic local-test-access", "Bearer invalid", "Bearer contains whitespace"]:
        with pytest.raises(HubAccessError) as error:
            auth.authenticate(header)
        assert error.value.status == 401
    with pytest.raises(ValueError, match="static credentials"):
        HubAuth({"mode": "jwt", "grants": [row]})


def test_duplicate_principal_grants_rejected():
    row = grant(token_sha256="a" * 64)
    with pytest.raises(ValueError, match="duplicate"):
        HubAuth({"mode": "development", "grants": [row, row]})


def test_principal_read_and_write_permissions_are_independent():
    principal = HubPrincipal("t", "u", "c", "user", frozenset({"alpha"}),
                             frozenset({"agent.sessions:write"}))
    principal.require("agent.sessions:write", "alpha")
    with pytest.raises(HubAccessError):
        principal.require("agent.sessions:read", "alpha")
