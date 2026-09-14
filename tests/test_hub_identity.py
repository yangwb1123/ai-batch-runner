"""Explicit identity mappings and fail-closed Aero-ID membership checks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pbatch.hub_ecosystem_http import IntegrationError
from pbatch.hub_errors import HubAccessError
from pbatch.hub_identity import IdentityGate


class _Transport:
    def __init__(self, response=None, error=None):
        self.response = response or {"data": {"eligible": True, "tenant_id": "team-a",
                                                "account_id": "account-a"}}
        self.error = error
        self.calls = []

    def request(self, method, path, *, body):
        self.calls.append((method, path, body))
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def settings(tmp_path):
    token = tmp_path / "aero-id.token"
    token.write_text("machine-token\n", encoding="utf-8")
    token.chmod(0o600)
    return [{"tenant_id": "team-a", "base_url": "https://identity.example",
             "token_file": str(token), "bindings": [
                 {"subject": "pairwise-subject", "client_id": "console", "account_id": "account-a"}
             ]}]


def _factory(transport):
    def create(*_args, **_kwargs):
        return transport
    return create


def test_empty_identity_list_disables_membership_gate():
    gate = IdentityGate([])
    assert gate.check_actor("unconfigured", "any-subject", "any-client") is True
    assert gate.check(SimpleNamespace(kind="user", tenant_id="t", subject="s", client_id="c")) is True


def test_configured_tenant_uses_only_explicit_subject_client_account_mapping(settings):
    transport = _Transport()
    gate = IdentityGate(settings, transport_factory=_factory(transport))
    principal = SimpleNamespace(kind="user", tenant_id="team-a",
                                subject="pairwise-subject", client_id="console")
    assert gate.check(principal) is True
    assert transport.calls == [("POST", "/internal/v1/agent/membership-check",
                                {"account_id": "account-a"})]


@pytest.mark.parametrize("tenant,subject,client", [
    ("other-tenant", "pairwise-subject", "console"),
    ("team-a", "another-subject", "console"),
    ("team-a", "pairwise-subject", "another-client"),
])
def test_missing_exact_mapping_denies_without_upstream_call(settings, tenant, subject, client):
    transport = _Transport()
    gate = IdentityGate(settings, transport_factory=_factory(transport))
    with pytest.raises(HubAccessError) as error:
        gate.check_actor(tenant, subject, client)
    assert error.value.code == "identity_unmapped"
    assert error.value.status == 403
    assert transport.calls == []


def test_gateway_identity_skips_direct_membership_but_actor_is_checked(settings):
    transport = _Transport()
    gate = IdentityGate(settings, transport_factory=_factory(transport))
    gateway = SimpleNamespace(kind="gateway", tenant_id="team-a",
                              subject="gateway", client_id="gateway-client")
    assert gate.check(gateway) is True
    assert transport.calls == []
    assert gate.check_actor("team-a", "pairwise-subject", "console") is True
    assert len(transport.calls) == 1


def test_aero_id_membership_denial_is_forbidden(settings):
    transport = _Transport({"data": {"eligible": False, "tenant_id": "team-a",
                                       "account_id": "account-a"}})
    gate = IdentityGate(settings, transport_factory=_factory(transport))
    with pytest.raises(HubAccessError) as error:
        gate.check_actor("team-a", "pairwise-subject", "console")
    assert (error.value.code, error.value.status) == ("membership_required", 403)


@pytest.mark.parametrize("response", [
    {"data": {"eligible": True, "tenant_id": "team-b", "account_id": "account-a"}},
    {"data": {"eligible": True, "tenant_id": "team-a", "account_id": "wrong-account"}},
    {"data": {"eligible": 1, "tenant_id": "team-a", "account_id": "account-a"}},
    {"data": {"eligible": True, "tenant_id": "team-a", "account_id": "account-a", "extra": True}},
    {"eligible": True},
])
def test_malformed_or_cross_tenant_response_fails_closed(settings, response):
    gate = IdentityGate(settings, transport_factory=_factory(_Transport(response)))
    with pytest.raises(HubAccessError) as error:
        gate.check_actor("team-a", "pairwise-subject", "console")
    assert (error.value.code, error.value.status) == ("identity_unavailable", 503)


def test_upstream_failure_has_no_stale_fallback(settings):
    gate = IdentityGate(settings, transport_factory=_factory(_Transport(error=IntegrationError("transport_failed"))))
    with pytest.raises(HubAccessError) as error:
        gate.check_actor("team-a", "pairwise-subject", "console")
    assert (error.value.code, error.value.status) == ("identity_unavailable", 503)


def test_configured_tenant_without_explicit_bindings_is_not_disabled(settings):
    settings[0]["bindings"] = []
    with pytest.raises(ValueError, match="explicit bindings"):
        IdentityGate(settings, transport_factory=_factory(_Transport()))


def test_duplicate_tenant_and_principal_mapping_are_rejected(settings):
    duplicate_mapping = [settings[0]["bindings"][0], settings[0]["bindings"][0]]
    settings[0]["bindings"] = duplicate_mapping
    with pytest.raises(ValueError, match="duplicate identity subject/client"):
        IdentityGate(settings, transport_factory=_factory(_Transport()))

    settings[0]["bindings"] = [{"subject": "s", "client_id": "c", "account_id": "a"}]
    with pytest.raises(ValueError, match="duplicate identity tenant"):
        IdentityGate(settings + [dict(settings[0])], transport_factory=_factory(_Transport()))


def test_invalid_or_insecure_service_credential_fails_at_configuration(settings):
    settings[0]["unknown"] = True
    with pytest.raises(ValueError, match="unsupported settings"):
        IdentityGate(settings, transport_factory=_factory(_Transport()))
