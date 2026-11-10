"""Bounded, pinned-issuer JWT verification for the optional Agent Hub service."""

from __future__ import annotations

import json
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .hub_errors import HubAccessError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HubAccessError("issuer_unavailable", "JWKS redirects are forbidden", 503)


def _https_url(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or parsed.query):
        raise ValueError(f"{label} must be an HTTPS URL without credentials/query/fragment")
    return value


class SnaplinkVerifier:
    """Fetch only the configured JWKS, with TTL, key-count and byte limits.

    Key URLs in JWT headers are never used. Unknown kids trigger a bounded
    refresh; failed/expired fetches fail closed. Sender-constrained tokens are
    rejected until the corresponding DPoP/mTLS proof validator is implemented.
    """

    def __init__(self, settings: dict, *, fetcher=None, clock=time.monotonic):
        try:
            import jwt
        except ImportError as exc:
            raise ValueError('JWT mode requires pip install "ai-batch-runner[hub]"') from exc
        self._jwt = jwt
        self.issuer = _https_url(settings.get("issuer", ""), "issuer")
        self.jwks_url = _https_url(settings.get("jwks_url", ""), "jwks_url")
        self.audience = settings.get("audience", "agent-hub")
        if not isinstance(self.audience, str) or not self.audience:
            raise ValueError("audience must be a nonempty string")
        self.algorithms = settings.get("algorithms", ["EdDSA", "RS256", "ES256"])
        if (not isinstance(self.algorithms, list) or not self.algorithms
                or not set(self.algorithms) <= {"EdDSA", "RS256", "ES256"}):
            raise ValueError("only EdDSA, RS256 and ES256 access tokens are supported")
        self._fetcher = fetcher or self._fetch
        self._clock = clock
        self._keys = {}
        self._expires = 0.0
        self._last_refresh = float("-inf")
        self._lock = threading.Lock()
        context = ssl.create_default_context(cafile=settings.get("ca_file") or None)
        self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))

    def _fetch(self) -> dict:
        try:
            with self._opener.open(Request(self.jwks_url), timeout=5) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError("JWKS response limit exceeded")
            return json.loads(raw)
        except (OSError, ValueError, URLError, HTTPError) as exc:
            raise HubAccessError("issuer_unavailable", "JWKS fetch failed", 503) from exc

    def _refresh(self) -> None:
        self._last_refresh = self._clock()
        document = self._fetcher()
        rows = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
            raise HubAccessError("issuer_unavailable", "invalid JWKS key set", 503)
        keys = {}
        for row in rows:
            if not isinstance(row, dict):
                raise HubAccessError("issuer_unavailable", "invalid JWKS key", 503)
            kid = row.get("kid")
            if not isinstance(kid, str) or not kid or len(kid) > 256 or kid in keys:
                raise HubAccessError("issuer_unavailable", "invalid JWKS key id", 503)
            keys[kid] = row
        self._keys = keys
        self._expires = self._clock() + 300

    def _key(self, header: dict):
        algorithm, kid = header.get("alg"), header.get("kid")
        if (algorithm not in self.algorithms or not isinstance(kid, str)
                or not kid or len(kid) > 256):
            raise HubAccessError("unauthorized", "unsupported token signing key", 401)
        with self._lock:
            stale = self._clock() >= self._expires
            if stale and self._clock() - self._last_refresh < 5:
                raise HubAccessError("issuer_unavailable", "JWKS refresh temporarily unavailable", 503)
            if stale or (kid not in self._keys and self._clock() - self._last_refresh >= 5):
                self._refresh()
            row = self._keys.get(kid)
            if row is None or row.get("alg", algorithm) != algorithm:
                raise HubAccessError("unauthorized", "unknown token signing key", 401)
            if row.get("use", "sig") != "sig" or "verify" not in row.get("key_ops", ["verify"]):
                raise HubAccessError("unauthorized", "invalid token signing key use", 401)
            return self._jwt.PyJWK.from_dict(row, algorithm=algorithm).key

    def verify(self, token: str) -> dict:
        try:
            header = self._jwt.get_unverified_header(token)
            if header.get("crit") or header.get("typ", "JWT") not in {"JWT", "at+jwt"}:
                raise HubAccessError("unauthorized", "unsupported access token type", 401)
            claims = self._jwt.decode(
                token, self._key(header), algorithms=self.algorithms,
                audience=self.audience, issuer=self.issuer,
                options={"require": ["exp", "iss", "aud", "sub", "tenant_id", "client_id"]})
            if any(not isinstance(claims.get(k), str) or not claims[k]
                   for k in ("sub", "tenant_id", "client_id")):
                raise HubAccessError("unauthorized", "invalid access token identity", 401)
            if "cnf" in claims or "act" in claims:
                raise HubAccessError("unauthorized", "token proof or delegation is unsupported", 401)
            return claims
        except self._jwt.PyJWTError as exc:
            raise HubAccessError("unauthorized", "invalid access token", 401) from exc
