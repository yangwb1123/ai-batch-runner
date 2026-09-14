"""Fail-closed Hub adapter for Aero-ID's bounded membership projection."""

from __future__ import annotations

from dataclasses import dataclass

from .hub_ecosystem_http import IntegrationError, JSONTransport, private_token
from .hub_errors import HubAccessError
from .hub_store_helpers import identifier


_PATH = "/internal/v1/agent/membership-check"
_MAX_TENANTS = 100
_MAX_BINDINGS_PER_TENANT = 1000


@dataclass(frozen=True)
class _Binding:
    account_id: str
    tenant: str
    transport: object


class IdentityGate:
    """Map explicit Hub identities to Aero-ID accounts and check membership.

    An empty settings list deliberately disables the optional integration.
    Once any tenant is configured, missing tenant and principal mappings deny.
    """

    def __init__(self, settings, *, transport_factory=None):
        if not isinstance(settings, list) or len(settings) > _MAX_TENANTS:
            raise ValueError("identity must be a list of up to 100 tenant connections")
        self.enabled = bool(settings)
        self._tenants = {}
        self._bindings = {}
        factory = transport_factory or JSONTransport
        for row in settings:
            self._add_tenant(row, factory)

    def _add_tenant(self, row, factory):
        allowed = {"tenant_id", "base_url", "token_file", "timeout", "ca_file",
                   "cert_file", "key_file", "bindings"}
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError("identity tenant contains unsupported settings")
        tenant = identifier(row.get("tenant_id"), "identity tenant_id")
        if tenant in self._tenants:
            raise ValueError("duplicate identity tenant")
        token_file = identifier(row.get("token_file"), "identity token_file", 4096)
        try:
            private_token(token_file)
        except IntegrationError:
            raise ValueError("identity service credential is unavailable or not private") from None
        transport = factory(row.get("base_url", ""), token_file,
                            timeout=row.get("timeout", 5.0),
                            ca_file=row.get("ca_file", ""),
                            cert_file=row.get("cert_file", ""),
                            key_file=row.get("key_file", ""),
                            max_response_bytes=262144)
        rows = row.get("bindings")
        if not isinstance(rows, list) or not 1 <= len(rows) <= _MAX_BINDINGS_PER_TENANT:
            raise ValueError("each identity tenant requires 1..1000 explicit bindings")
        for binding in rows:
            value = _mapping(binding)
            key = (tenant, value[0], value[1])
            if key in self._bindings:
                raise ValueError("duplicate identity subject/client binding")
            self._bindings[key] = _Binding(value[2], tenant, transport)
        self._tenants[tenant] = transport

    def check(self, principal) -> bool:
        """Check a Hub user; Gateway credentials have no human membership."""
        if not self.enabled or principal.kind == "gateway":
            return True
        return self.check_actor(principal.tenant_id, principal.subject, principal.client_id)

    def check_actor(self, tenant: str, subject: str, client_id: str) -> bool:
        """Check an explicitly mapped actor, including Gateway-derived turns."""
        if not self.enabled:
            return True
        tenant = _principal_text(tenant, "tenant_id")
        subject = _principal_text(subject, "subject")
        client_id = _principal_text(client_id, "client_id")
        if tenant not in self._tenants:
            raise _denied("identity_unmapped", "no Aero-ID identity binding is configured")
        binding = self._bindings.get((tenant, subject, client_id))
        if binding is None:
            raise _denied("identity_unmapped", "no Aero-ID identity binding is configured")
        try:
            response = binding.transport.request("POST", _PATH,
                                                 body={"account_id": binding.account_id})
        except IntegrationError:
            raise _unavailable() from None
        if not _eligible_response(response, tenant, binding.account_id):
            raise _unavailable()
        if not response["data"]["eligible"]:
            raise _denied("membership_required", "active membership is required")
        return True


def _mapping(value):
    if not isinstance(value, dict) or set(value) != {"subject", "client_id", "account_id"}:
        raise ValueError("identity binding requires subject, client_id, and account_id")
    subject = identifier(value["subject"], "identity subject")
    client_id = identifier(value["client_id"], "identity client_id")
    account_id = identifier(value["account_id"], "identity account_id")
    return subject, client_id, account_id


def _principal_text(value, label):
    try:
        return identifier(value, label)
    except ValueError:
        raise _denied("identity_unmapped", "no Aero-ID identity binding is configured") from None


def _eligible_response(value, tenant, account_id):
    if not isinstance(value, dict) or set(value) != {"data"}:
        return False
    data = value["data"]
    return (isinstance(data, dict) and set(data) == {"eligible", "tenant_id", "account_id"}
            and type(data["eligible"]) is bool and data["tenant_id"] == tenant
            and data["account_id"] == account_id)


def _denied(code, message):
    return HubAccessError(code, message, 403)


def _unavailable():
    return HubAccessError("identity_unavailable", "Aero-ID membership check is unavailable", 503)


__all__ = ["IdentityGate"]
