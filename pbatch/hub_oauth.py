"""In-memory, single-flight machine tokens for long-lived Agent Hub clients."""

from __future__ import annotations

import math
import threading
import time
import urllib.request

from .hub_oauth_config import CONFIG_ERROR, load_config
from .hub_oauth_http import NoRedirect, TokenExchangeError, exchange


class MachineTokenProvider:
    """Renew fixed-resource credentials with bounded backoff and no disk cache."""

    def __init__(self, path, *, hub_url: str, timeout: float = 5.0):
        if (type(timeout) not in {int, float} or not 0 < timeout <= 120
                or not math.isfinite(timeout)):
            raise ValueError(CONFIG_ERROR)
        self._config = load_config(path, hub_url)
        self._timeout = float(timeout)
        self._clock = time.monotonic
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = self._refresh_at = self._retry_at = 0.0
        self._failure = None
        self._rejected_until = 0.0
        self._failures = 0

    @classmethod
    def from_file(cls, path, *, hub_url: str, timeout: float = 5.0):
        return cls(path, hub_url=hub_url, timeout=timeout)

    def __repr__(self) -> str:
        return "MachineTokenProvider(credentials=<redacted>)"

    def __call__(self) -> str:
        with self._lock:
            now = self._clock()
            if now < self._rejected_until:
                raise TokenExchangeError("machine_auth_rejected", status=401) from None
            if self._token and now < self._refresh_at:
                return self._token
            if self._failure is not None and now < self._retry_at:
                return self._after_failure(now)
            return self._renew(now)

    def invalidate(self, token: str) -> None:
        """A delayed 401 may invalidate its token, never a newer credential."""
        with self._lock:
            if self._token and token == self._token:
                self._rejected_until = self._clock() + 5.0
                self._token = ""
                self._expires_at = self._refresh_at = 0.0

    def _renew(self, started: float) -> str:
        try:
            token, ttl = exchange(self._config, self._opener, self._timeout)
            now = self._clock()
            expiry = started + ttl
            if now >= expiry:
                raise TokenExchangeError("invalid_token_response")
        except TokenExchangeError as exc:
            self._remember_failure(exc)
            return self._after_failure(self._clock())
        self._token, self._expires_at = token, expiry
        refresh = expiry - min(60.0, ttl * 0.1)
        self._refresh_at = max(refresh, now + min(1.0, (expiry - now) / 2))
        self._failure, self._failures, self._retry_at = None, 0, 0.0
        self._rejected_until = 0.0
        return token

    def _remember_failure(self, error: TokenExchangeError) -> None:
        self._failure = (error.code, error.status, error.transient)
        self._failures = min(self._failures + 1, 6)
        self._retry_at = self._clock() + min(30.0, 2 ** (self._failures - 1))
        if not error.transient:
            self._token = ""
            self._expires_at = self._refresh_at = 0.0

    def _after_failure(self, now: float) -> str:
        code, status, transient = self._failure
        if transient and self._token and now < self._expires_at:
            return self._token
        raise TokenExchangeError(code, status=status, transient=transient) from None


__all__ = ["MachineTokenProvider"]
