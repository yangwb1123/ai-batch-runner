"""Shared short-lived user credentials, with login/logout fencing and no refresh."""

from __future__ import annotations

from contextlib import contextmanager
import hmac
import json
import math
import secrets
import time

from .hub_client import HubError
from .hub_oauth_config import strict_json
from .hub_user_auth_config import load_user_config, profile_digest, validate_user_config
from .hub_user_credential_files import transaction, unavailable

MAX_TOKEN_BYTES = 16 * 1024
MAX_TOKEN_TTL = 86400


def _required() -> HubError:
    return HubError("sign in to the Agent Hub again", code="user_login_required", status=401)


def _ticket(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _timestamp(value) -> bool:
    return (type(value) in {int, float} and 0 < value <= 253402300799
            and math.isfinite(value))


def _credential(value):
    if not isinstance(value, dict) or set(value) != {"access_token", "issued_at", "expires_at"}:
        raise unavailable()
    token = value["access_token"]
    if (not isinstance(token, str) or not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES
            or not token.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in token)):
        raise unavailable()
    issued, expires = value["issued_at"], value["expires_at"]
    if not _timestamp(issued) or not _timestamp(expires):
        raise unavailable()
    if not 0 < issued < expires or not 0 < expires - issued <= MAX_TOKEN_TTL:
        raise unavailable()
    return dict(value)


def _state(raw, profile):
    if raw is None:
        return {"version": 1, "profile": profile, "pending": "", "credential": None}
    try:
        value = strict_json(raw)
        if not isinstance(value, dict) or set(value) != {"version", "profile", "pending", "credential"}:
            raise unavailable()
        if type(value["version"]) is not int or value["version"] != 1 or value["profile"] != profile:
            raise unavailable()
        if value["pending"] != "" and not _ticket(value["pending"]):
            raise unavailable()
        if value["credential"] is not None:
            value["credential"] = _credential(value["credential"])
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise unavailable() from None


def _save(item, state):
    item.write(json.dumps(state, ensure_ascii=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8"))


class CredentialStore:
    """Each operation opens and locks the current directory; tokens are never cached."""

    def __init__(self, config):
        self._config = validate_user_config(config)
        self._profile = profile_digest(self._config)
        self.hub_url = self._config["hub_url"]
        self._directory = self._config["credential_directory"]
        self._clock = time.time
        self._lock_timeout = 5.0

    def __repr__(self):
        return "CredentialStore(credentials=<redacted>)"

    @contextmanager
    def _transaction(self, *, create=False):
        with transaction(self._directory, create=create, timeout=self._lock_timeout) as item:
            state = _state(item.read() if item else None, self._profile)
            yield item, state

    def begin_login(self) -> str:
        with self._transaction(create=True) as (item, state):
            ticket = secrets.token_hex(32)
            state["pending"] = ticket
            _save(item, state)
            return ticket

    def finish_login(self, ticket: str, credential: dict) -> None:
        with self._transaction() as (item, state):
            if item is None or not _ticket(ticket) or not hmac.compare_digest(state["pending"], ticket):
                raise HubError("login was cancelled or superseded", code="user_login_superseded")
            checked = _credential(credential)
            now = self._now()
            if now < checked["issued_at"]:
                raise unavailable()
            if now >= checked["expires_at"]:
                raise _required()
            state.update(pending="", credential=checked)
            _save(item, state)

    def cancel_login(self, ticket: str) -> None:
        with self._transaction() as (item, state):
            if item is not None and _ticket(ticket) and hmac.compare_digest(state["pending"], ticket):
                state["pending"] = ""
                _save(item, state)

    def logout(self) -> None:
        with self._transaction() as (item, state):
            if item is not None:
                state.update(pending="", credential=None)
                _save(item, state)

    def _now(self) -> float:
        now = self._clock()
        if not _timestamp(now):
            raise unavailable()
        return now

    def _current(self, item, state):
        credential = state["credential"]
        if credential is None:
            return None
        now = self._now()
        if now < credential["issued_at"]:
            raise unavailable()
        if now >= credential["expires_at"]:
            state["credential"] = None
            _save(item, state)
            return None
        return credential

    def status(self) -> dict:
        with self._transaction() as (item, state):
            credential = self._current(item, state)
            result = {"authenticated": credential is not None, "pending": bool(state["pending"])}
            if credential is not None:
                result["expires_at"] = credential["expires_at"]
            return result

    def token(self) -> str:
        with self._transaction() as (item, state):
            credential = self._current(item, state)
            if credential is None:
                raise _required()
            return credential["access_token"]

    def invalidate(self, token: str) -> None:
        with self._transaction() as (item, state):
            credential = state["credential"]
            if (item is not None and credential is not None and isinstance(token, str)
                    and hmac.compare_digest(credential["access_token"], token)):
                state["credential"] = None
                _save(item, state)


class UserTokenProvider:
    def __init__(self, store):
        self._store = store
        self.hub_url = store.hub_url

    @classmethod
    def from_file(cls, path, *, hub_url: str = "", timeout: float = 5):
        if type(timeout) not in {int, float} or not 0 < timeout <= 120 or not math.isfinite(timeout):
            raise ValueError("user credential timeout is invalid")
        store = CredentialStore(load_user_config(path, hub_url=hub_url))
        store._lock_timeout = float(timeout)
        return cls(store)

    def __repr__(self):
        return "UserTokenProvider(credentials=<redacted>)"

    def __call__(self) -> str:
        return self._store.token()

    def invalidate(self, token: str) -> None:
        self._store.invalidate(token)


__all__ = ["CredentialStore", "UserTokenProvider"]
