"""Opt-in, single-process browser sessions using the official Snaplink SDK.

The SDK owns PKCE/state/code exchange. A browser session is never authority:
every read reuses HubAuth, then HubService's tenant/resource/membership gates.
"""

from __future__ import annotations

import secrets
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .hub_errors import HubAccessError
from .hub_jwks import _https_url

READ_SCOPES = ("agent.instances:read", "agent.sessions:read")
PENDING_TTL = 600
SESSION_TTL = 1800
MAX_SESSIONS = 256


def _configuration(settings, auth):
    config = settings["web"]
    if not isinstance(config, dict) or set(config) != {"public_origin", "client_id", "tenant_id"}:
        raise ValueError("web requires only public_origin, client_id and tenant_id")
    for value in config.values():
        if (not isinstance(value, str) or not 1 <= len(value) <= 256
                or any(ord(char) < 33 or ord(char) == 127 for char in value)):
            raise ValueError("web settings require bounded nonempty text without whitespace")
    origin = _https_url(config["public_origin"], "web.public_origin")
    parsed = urlsplit(origin)
    if parsed.path or parsed.port == 0 or "\\" in origin:
        raise ValueError("web.public_origin must be an exact HTTPS origin")
    if origin not in settings.get("allowed_origins", []):
        raise ValueError("web.public_origin must be present in allowed_origins")
    if auth.mode != "jwt" or settings["auth"].get("mode", "jwt") != "jwt":
        raise ValueError("web requires jwt authentication")
    return dict(config)


@dataclass
class _Session:
    sdk: object = field(repr=False)
    expires: float
    pending: bool


class HubWeb:
    def __init__(self, settings, auth, *, transport=None, clock=time.monotonic):
        config = _configuration(settings, auth)
        try:
            from snaplink_sso import Snaplink, SSOError
        except ImportError as exc:
            raise ValueError('browser login requires pip install "abr[web]"') from exc
        from .hub_web_transport import SDKTransport
        self.origin = config["public_origin"]
        self.host = urlsplit(self.origin).netloc
        self.client_id, self.tenant_id = config["client_id"], config["tenant_id"]
        self.auth, self._clock = auth, clock
        self._sdk, self._sdk_error = Snaplink, SSOError
        issuer = _https_url(settings["auth"]["issuer"], "web issuer")
        self.login_origin = "https://" + urlsplit(issuer).netloc
        if not re.fullmatch(r"https://[A-Za-z0-9.\-:\[\]]+", self.login_origin):
            raise ValueError("web issuer must have a CSP-safe HTTPS authority")
        self._transport = transport or SDKTransport(issuer)
        self._options = {"base_url": issuer, "client_id": self.client_id,
                         "redirect_uri": self.origin + "/auth/callback",
                         "return_to": self.origin + "/", "scope": ["openid", *READ_SCOPES],
                         "resource": [settings["auth"].get("audience", "agent-hub")],
                         "transaction_ttl_seconds": PENDING_TTL}
        self._sessions = {}
        self._closed = False
        self._lock = threading.Lock()

    def _expire(self):
        for key, session in list(self._sessions.items()):
            if self._clock() >= session.expires:
                self._sessions.pop(key).sdk.clear()

    def _remember(self, sdk, *, pending):
        with self._lock:
            if self._closed:
                sdk.clear()
                raise HubAccessError("web_closed", "browser service is closed", 503)
            self._expire()
            if len(self._sessions) >= MAX_SESSIONS:
                sdk.clear()
                raise HubAccessError("web_capacity", "browser session capacity reached", 429)
            key = secrets.token_urlsafe(32)
            ttl = PENDING_TTL if pending else SESSION_TTL
            self._sessions[key] = _Session(sdk, self._clock() + ttl, pending)
            return key

    def begin(self, previous):
        self.clear(previous)
        sdk = self._sdk(transport=self._transport)
        result = sdk.login(self._options)
        return self._remember(sdk, pending=True), result.redirect_url

    def _take_pending(self, key):
        with self._lock:
            self._expire()
            session = self._sessions.get(key)
            if session is None or not session.pending:
                raise HubAccessError("login_required", "login transaction unavailable", 401)
            return self._sessions.pop(key)

    def complete(self, key, callback):
        session = self._take_pending(key)  # consume before any network or SDK call
        try:
            if (not callback.get("state") or not callback.get("iss")
                    or bool(callback.get("code")) == bool(callback.get("error"))):
                raise ValueError("invalid callback")
            result = session.sdk.login({**self._options, "callback_params": callback})
            tokens = result.tokens
            if (not isinstance(tokens, dict) or "error" in tokens
                    or str(tokens.get("token_type", "")).lower() != "bearer"):
                raise ValueError("invalid token response")
            self._principal(session.sdk)
            if self._clock() >= session.expires:
                raise ValueError("expired callback")
            return self._remember(session.sdk, pending=False)
        except (self._sdk_error, ValueError, TypeError, RecursionError):
            session.sdk.clear()
            raise HubAccessError("login_failed", "Snaplink login failed; start again", 400) from None
        except HubAccessError:
            session.sdk.clear()
            raise

    def _principal(self, sdk):
        token = sdk.access_token
        if not isinstance(token, str):
            raise HubAccessError("login_required", "login required", 401)
        principal = self.auth.authenticate("Bearer " + token)
        if (principal.kind != "user" or principal.tenant_id != self.tenant_id
                or principal.client_id != self.client_id):
            raise HubAccessError("forbidden", "browser identity binding mismatch")
        for scope in READ_SCOPES:
            principal.require(scope)
        return principal

    def principal(self, key):
        with self._lock:
            self._expire()
            session = self._sessions.get(key)
            if session is None or session.pending:
                raise HubAccessError("login_required", "login required", 401)
        try:
            principal = self._principal(session.sdk)
            with self._lock:
                self._expire()
                if self._sessions.get(key) is not session:
                    raise HubAccessError("login_required", "browser session ended", 401)
            return principal
        except HubAccessError:
            self.clear(key)
            raise

    def clear(self, key):
        with self._lock:
            session = self._sessions.pop(key, None)
            if session is not None:
                session.sdk.clear()  # ABR logout is not global SSO logout or task cancellation

    def close(self):
        with self._lock:
            self._closed = True
            for session in self._sessions.values():
                session.sdk.clear()
            self._sessions.clear()
