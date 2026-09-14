"""Agent Hub identities: Snaplink access tokens plus explicit resource grants.

Development credentials are opt-in, hashed, and restricted to loopback by the
listener. Production authority comes from a verified issuer, never request JSON.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any

from .hub_errors import HubAccessError
from .hub_compute_models import COMPUTE_SCOPES
from .hub_integration_models import SCOPES as INTEGRATION_SCOPES


USER_SCOPES = frozenset({
    "agent.instances:read", "agent.sessions:read", "agent.sessions:write",
    "agent.turns:cancel",
}) | COMPUTE_SCOPES | INTEGRATION_SCOPES
GATEWAY_SCOPE = "agent.gateway:connect"


@dataclass(frozen=True)
class HubPrincipal:
    tenant_id: str
    subject: str
    client_id: str
    kind: str
    instances: frozenset[str]
    scopes: frozenset[str]
    compute_instances: frozenset[str] | None = None

    @property
    def compute_targets(self) -> frozenset[str]:
        return self.instances if self.compute_instances is None else self.compute_instances

    def allows_compute(self, instance_id: str) -> bool:
        return "*" in self.compute_targets or instance_id in self.compute_targets

    def require(self, scope: str, instance_id: str = "") -> None:
        if scope not in self.scopes:
            raise HubAccessError("insufficient_scope", "required scope is missing")
        if instance_id and not self.allows(instance_id):
            raise HubAccessError("forbidden", "instance access denied")

    def allows(self, instance_id: str) -> bool:
        return "*" in self.instances or instance_id in self.instances

    @property
    def instance_filter(self):
        return None if "*" in self.instances else tuple(sorted(self.instances))


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a nonempty string of at most 256 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _strings(value: Any, name: str) -> frozenset[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 200:
        raise ValueError(f"{name} must contain 1..200 strings")
    return frozenset(_text(item, name) for item in value)


def _grant(row: Any) -> HubPrincipal:
    if not isinstance(row, dict):
        raise ValueError("each grant must be an object")
    kind = row.get("kind")
    if kind not in {"user", "gateway"}:
        raise ValueError("grant kind must be user or gateway")
    scopes = _strings(row.get("scopes"), "scopes")
    expected = USER_SCOPES if kind == "user" else {GATEWAY_SCOPE} | COMPUTE_SCOPES
    if not scopes <= expected:
        raise ValueError("grant contains scopes outside its principal kind")
    instances = _strings(row.get("instances"), "instances")
    if kind == "gateway" and (len(instances) != 1 or "*" in instances):
        raise ValueError("gateway grant must bind exactly one instance")
    return HubPrincipal(
        _text(row.get("tenant_id"), "tenant_id"),
        _text(row.get("subject"), "subject"),
        _text(row.get("client_id"), "client_id"), kind, instances, scopes,
        _strings(row["compute_instances"], "compute_instances")
        if "compute_instances" in row else None)


class HubAuth:
    """Intersect signed token scopes with operator-configured instance grants."""

    def __init__(self, settings: dict, *, verifier=None):
        self.mode = settings.get("mode", "jwt")
        rows = settings.get("grants", [])
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise ValueError("auth.grants must contain 1..1000 grants")
        self.grants = [_grant(row) for row in rows]
        self._development = self._dev_credentials(rows)
        self._verifier = verifier
        if self.mode == "jwt":
            from .hub_jwks import SnaplinkVerifier
            self._verifier = verifier or SnaplinkVerifier(settings)
        elif self.mode != "development":
            raise ValueError("auth.mode must be jwt or development")
        keys = [(g.tenant_id, g.subject, g.client_id) for g in self.grants]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate auth principal grants")

    def _dev_credentials(self, rows: list) -> list[tuple[str, HubPrincipal]]:
        result = []
        for row, grant in zip(rows, self.grants):
            digest = row.get("token_sha256", "")
            if self.mode == "development":
                if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("development grant requires token_sha256")
                result.append((digest, grant))
            elif digest:
                raise ValueError("static credentials are only allowed in development mode")
        if len({item[0] for item in result}) != len(result):
            raise ValueError("duplicate development credential digest")
        return result

    def authenticate(self, header: str) -> HubPrincipal:
        if not isinstance(header, str) or not header.startswith("Bearer "):
            raise HubAccessError("unauthorized", "Bearer access token required", 401)
        token = header[7:]
        if not token or len(token) > 16384 or any(char.isspace() for char in token):
            raise HubAccessError("unauthorized", "invalid access token", 401)
        if self.mode == "development":
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            for expected, grant in self._development:
                if hmac.compare_digest(digest, expected):
                    return grant
            raise HubAccessError("unauthorized", "invalid access token", 401)
        return self._principal(self._verifier.verify(token))

    def _principal(self, claims: dict) -> HubPrincipal:
        key = (claims.get("tenant_id"), claims.get("sub"), claims.get("client_id"))
        grant = next((g for g in self.grants
                      if (g.tenant_id, g.subject, g.client_id) == key), None)
        if grant is None:
            raise HubAccessError("forbidden", "principal has no Agent Hub grant")
        raw_scopes = claims.get("scope", "")
        if not isinstance(raw_scopes, str):
            raise HubAccessError("unauthorized", "invalid access token scope", 401)
        scopes = grant.scopes & frozenset(raw_scopes.split())
        return HubPrincipal(grant.tenant_id, grant.subject, grant.client_id,
                            grant.kind, grant.instances, scopes, grant.compute_instances)
