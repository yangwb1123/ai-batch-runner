"""Opt-in, single-process browser sessions using the official Snaplink SDK.

The SDK owns PKCE/state/code exchange. A browser session is never authority:
every request reuses HubAuth, then HubService's tenant/resource/membership gates.
"""

from __future__ import annotations

import secrets
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .hub_auth import WEB_READ_SCOPES as READ_SCOPES
from .hub_errors import HubAccessError
from .hub_file_snapshot_models import READ_SCOPE as FILE_READ_SCOPE
from .hub_jwks import _https_url

PENDING_TTL = 600
SESSION_TTL = 1800
MAX_SESSIONS = 256
CONTROL_SCOPES = (*READ_SCOPES, "agent.sessions:write", "agent.turns:cancel")


def _feature_flags(config):
    for flag in ("control", "file_snapshots"):
        if not isinstance(config.get(flag, False), bool):
            raise ValueError(f"web.{flag} must be boolean")
    if config.get("file_snapshots", False) and not config.get("control", False):
        raise ValueError("web.file_snapshots requires web.control")


def _configuration(settings, auth):
    config = settings["web"]
    required = {"public_origin", "client_id", "tenant_id"}
    if not isinstance(config, dict) or not required <= set(config) or set(config) - required - {"control", "file_snapshots"}:
        raise ValueError("web requires public_origin, client_id, tenant_id and optional control/file_snapshots")
    _feature_flags(config)
    for value in (config[name] for name in required):
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
            raise ValueError('browser login requires pip install "ai-batch-runner[web]"') from exc
        from .hub_web_transport import SDKTransport
        self.origin = config["public_origin"]
        self.host = urlsplit(self.origin).netloc
        self.client_id, self.tenant_id = config["client_id"], config["tenant_id"]
        self.control = config.get("control", False)
        self.file_snapshots = config.get("file_snapshots", False)
        self.auth, self._clock = auth, clock
        self._sdk, self._sdk_error = Snaplink, SSOError
        issuer = _https_url(settings["auth"]["issuer"], "web issuer")
        self.login_origin = "https://" + urlsplit(issuer).netloc
        if not re.fullmatch(r"https://[A-Za-z0-9.\-:\[\]]+", self.login_origin):
            raise ValueError("web issuer must have a CSP-safe HTTPS authority")
        self._transport = transport or SDKTransport(issuer)
        self._options = {"base_url": issuer, "client_id": self.client_id,
                         "redirect_uri": self.origin + "/auth/callback",
                         "return_to": self.origin + "/",
                         "scope": ["openid", *(CONTROL_SCOPES if self.control else READ_SCOPES),
                                   *([FILE_READ_SCOPE] if self.file_snapshots else [])],
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

    def _principal(self, sdk, *, control=False):
        token = sdk.access_token
        if not isinstance(token, str):
            raise HubAccessError("login_required", "login required", 401)
        principal = self.auth.authenticate("Bearer " + token, web_read=not control)
        if (principal.kind != "user" or principal.tenant_id != self.tenant_id
                or principal.client_id != self.client_id):
            raise HubAccessError("forbidden", "browser identity binding mismatch")
        for scope in READ_SCOPES:
            principal.require(scope)
        return principal

    def principal(self, key, *, control=False):
        if control and not self.control:
            raise HubAccessError("web_control_disabled", "browser task control is not enabled")
        with self._lock:
            self._expire()
            session = self._sessions.get(key)
            if session is None or session.pending:
                raise HubAccessError("login_required", "login required", 401)
        try:
            principal = self._principal(session.sdk, control=control)
            with self._lock:
                self._expire()
                if self._sessions.get(key) is not session:
                    raise HubAccessError("login_required", "browser session ended", 401)
            return principal
        except HubAccessError:
            # An exact task grant may be absent while metadata-only policy remains valid.
            if not control:
                self.clear(key)
            raise

    def access(self, key):
        self.principal(key)
        principal = None
        if self.control:
            try:
                principal = self.principal(key, control=True)
            except HubAccessError as exc:
                if exc.status != 403:
                    raise
        scopes = (*CONTROL_SCOPES, *([FILE_READ_SCOPE] if self.file_snapshots else []))
        access = {"mode": "control" if self.control else "read_only",
                "task_access": principal is not None,
                "scopes": sorted(principal.scopes.intersection(scopes)) if principal else [],
                "context": self.context(key)}
        if self.file_snapshots:
            access["file_snapshots"] = bool(principal and FILE_READ_SCOPE in principal.scopes)
        return access

    @staticmethod
    def context(key):
        """Non-authorizing binding: a delayed write cannot adopt another login's Cookie."""
        import hashlib
        return hashlib.sha256(key.encode("ascii")).hexdigest()

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
