"""Opt-in tenant-wide Web readers; real signatures/SDK, synthetic issuer only."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from pbatch.hub_auth import HubAuth
from pbatch.hub_errors import HubAccessError
from test_hub_auth import grant, signed
from test_hub_web import web_env
from test_hub_web_http import browser, sign_in

READ_SCOPES = frozenset({"agent.instances:read", "agent.sessions:read"})


def policy(**changes):
    result = {"tenant_id": "team", "client_id": "console", "instances": ["alpha"]}
    result.update(changes)
    return result


def readers(verifier, **changes):
    settings = {"mode": "jwt", "grants": [], "web_read_grants": [policy()]}
    settings.update(changes)
    return HubAuth(settings, verifier=verifier)


@pytest.mark.parametrize("subject", ["alice", "bob", "pairwise-user-32"])
def test_all_signed_users_in_exact_tenant_and_client_are_web_readers(signed, subject):
    _, token, verifier, *_ = signed
    auth = readers(verifier)
    principal = auth.authenticate("Bearer " + token(
        sub=subject, scope=" ".join(sorted(READ_SCOPES)) + " agent.sessions:write admin:write"),
        web_read=True)
    assert principal.subject == subject and principal.kind == "user"
    assert principal.tenant_id == "team" and principal.client_id == "console"
    assert principal.instances == {"alpha"} and principal.scopes == READ_SCOPES
    assert not principal.allows_compute("alpha")
    for scope in ("agent.sessions:write", "agent.turns:cancel", "agent.gateway:connect", "admin:write"):
        with pytest.raises(HubAccessError):
            principal.require(scope, "alpha")
    with pytest.raises(HubAccessError):
        principal.require("agent.sessions:read", "private-beta")


def test_web_policy_is_not_an_original_bearer_api_grant(signed):
    _, token, verifier, *_ = signed
    auth = readers(verifier)
    with pytest.raises(HubAccessError) as error:
        auth.authenticate("Bearer " + token(scope=" ".join(READ_SCOPES)))
    assert error.value.status == 403


@pytest.mark.parametrize("claims", [
    {"tenant_id": "other"}, {"client_id": "other-client"}, {"sub": "console"},
    {"sub": ""}, {"sub": "x" * 257}, {"sub": "bad\nsubject"},
    {"iss": "https://other.test"}, {"aud": "other"}, {"exp": 1},
    {"act": {"sub": "delegator"}}, {"cnf": {"jkt": "proof-required"}},
])
def test_web_reader_does_not_bypass_identity_or_proof_checks(signed, claims):
    _, token, verifier, *_ = signed
    auth = readers(verifier)
    with pytest.raises(HubAccessError) as error:
        auth.authenticate("Bearer " + token(**claims), web_read=True)
    assert error.value.status in {401, 403}


def test_reader_scopes_are_still_intersected_with_signed_scopes(signed):
    _, token, verifier, *_ = signed
    principal = readers(verifier).authenticate(
        "Bearer " + token(scope="agent.sessions:read"), web_read=True)
    assert principal.scopes == {"agent.sessions:read"}
    with pytest.raises(HubAccessError):
        principal.require("agent.instances:read")


def test_explicit_principal_grant_takes_precedence_without_union(signed):
    auth, token, verifier, *_ = signed
    row = grant()
    row["scopes"] = ["agent.sessions:read"]
    reader = readers(verifier, grants=[row], web_read_grants=[policy(instances=["alpha", "beta"])])
    principal = reader.authenticate("Bearer " + token(), web_read=True)
    assert principal.instances == {"alpha"} and principal.scopes == {"agent.sessions:read"}
    with pytest.raises(HubAccessError):
        principal.require("agent.sessions:read", "beta")
    assert auth.authenticate("Bearer " + token()).subject == "alice"


@pytest.mark.parametrize("bad", [None, False, {}, "all", [None], [],
    [policy(tenant_id="*")], [policy(client_id="*")], [policy(tenant_id="team ")],
    [policy(subject="*")], [policy(scopes=["agent.sessions:write"])],
    [policy(kind="gateway")], [policy(token_sha256="a" * 64)],
    [policy(instances=[])], [policy(instances="*")], [policy(instances=[1])],
    [policy(instances=["*"])], [policy(instances=["alpha", "*"])],
    [policy(), policy()],
])
def test_invalid_or_implicit_web_policies_fail_closed(signed, bad):
    _, _, verifier, *_ = signed
    with pytest.raises(ValueError):
        readers(verifier, web_read_grants=bad)


def test_web_policy_rejected_in_development_and_counts_toward_existing_budget(signed):
    _, _, verifier, *_ = signed
    row = grant(token_sha256="a" * 64)
    with pytest.raises(ValueError):
        readers(verifier, mode="development", grants=[row])
    policies = [policy(client_id=f"console-{i}") for i in range(1000)]
    with pytest.raises(ValueError):
        readers(verifier, grants=[grant()], web_read_grants=policies)
    with pytest.raises(ValueError):
        readers(verifier, web_read_grants=policies + [policy()])


def test_default_subject_star_stays_literal_and_empty_configuration_stays_closed(signed):
    auth, token, verifier, *_ = signed
    auth.grants = [replace(auth.grants[0], subject="*")]
    with pytest.raises(HubAccessError):
        auth.authenticate("Bearer " + token(sub="bob"))
    auth.grants = [replace(auth.grants[0], subject="alice", instances=frozenset({"*"}))]
    assert auth.authenticate("Bearer " + token()).allows("private-beta")
    with pytest.raises(ValueError):
        HubAuth({"mode": "jwt", "grants": []}, verifier=verifier)


@pytest.mark.parametrize("subject", ["alice", "bob"])
def test_real_sdk_http_reader_is_scoped_read_only_and_reauthorized(browser, signed, subject):
    call, server, (web, _, transport, _, _) = browser
    _, token, verifier, *_ = signed
    auth = readers(verifier)
    web.auth = server.auth = auth
    transport.token = lambda: token(sub=subject, scope=" ".join(READ_SCOPES) + " agent.sessions:write")
    assert call("/web/api/instances")[0] == 401
    cookie, _, _ = sign_in(call)
    status, _, body = call("/web/api/instances", cookie=cookie)
    assert status == 200 and [row["instance_id"] for row in json.loads(body)["data"]] == ["alpha"]
    assert call("/web/api/sessions?instance_id=private-beta", cookie=cookie)[0] == 403
    assert call("/web/api/sessions", method="POST", cookie=cookie,
                headers={"Origin": web.origin})[0] == 405
    assert call("/api/v1/agent/instances", cookie=cookie)[0] == 401
    assert call("/api/v1/agent/instances",
                headers={"Authorization": "Bearer " + transport.token()})[0] == 403
    auth.web_read_grants.clear()
    assert call("/web/api/instances", cookie=cookie)[0] == 403
    assert call("/web/api/instances", cookie=cookie)[0] == 401
