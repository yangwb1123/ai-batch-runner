"""Short-lived public-client device login for CLI and TUI users."""

from __future__ import annotations

import math
import time
import urllib.parse

from .hub_device_oauth_http import UserAuthError, new_opener, post_form
from .hub_oauth_config import _text
from .hub_oauth_http import MAX_TTL, _matching_scope, _valid_token
from .hub_user_auth_config import CONFIG_ERROR, endpoint_origin, validate_user_config

MAX_CHALLENGE_TTL = 1800
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceAuthorization:
    """Bind one device grant to its configured issuer and original deadline."""

    def __init__(self, config: dict, timeout: float = 5.0):
        if (type(timeout) not in {float, int} or not 0 < timeout <= 120
                or not math.isfinite(timeout)):
            raise ValueError(CONFIG_ERROR)
        self._config = validate_user_config(config)
        self._timeout = float(timeout)
        self._opener = new_opener()
        self._clock, self._wall_clock, self._sleep = time.monotonic, time.time, time.sleep
        self._started = False
        self._challenge = None
        self._deadline = 0.0

    def __repr__(self):
        return "DeviceAuthorization(credentials=<redacted>)"

    def start(self) -> dict:
        if self._started:
            raise UserAuthError("invalid_user_auth_challenge")
        self._started = True
        started = self._clock()
        status, value = post_form(self._opener, self._config["device_authorization_endpoint"], {
            "client_id": self._config["client_id"], "resource": self._config["resource"],
            "scope": " ".join(self._config["scopes"]),
        }, self._timeout)
        if status != 200:
            raise UserAuthError("user_auth_rejected", status=status)
        challenge = _challenge(value, self._config["verification_origin"])
        self._deadline = started + challenge["expires_in"]
        if self._clock() >= self._deadline:
            raise UserAuthError("user_auth_expired")
        self._challenge = dict(challenge)
        return challenge

    def poll(self, challenge: dict) -> dict:
        if self._challenge is None or challenge != self._challenge:
            raise UserAuthError("invalid_user_auth_challenge")
        current, self._challenge = self._challenge, None
        interval = float(current["interval"])
        while True:
            self._wait(interval)
            started, wall = self._clock(), self._wall_clock()
            try:
                status, value = self._poll_once(current["device_code"])
            except UserAuthError as error:
                if not error.retryable:
                    raise
                interval = min(MAX_CHALLENGE_TTL, interval * 2)
                continue
            if self._clock() >= self._deadline:
                raise UserAuthError("user_auth_expired")
            if status == 200:
                return _credential(value, self._config["scopes"], wall, self._clock() - started)
            interval = _poll_interval(value, interval)

    def _poll_once(self, device_code: str) -> tuple[int, dict]:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise UserAuthError("user_auth_expired")
        return post_form(self._opener, self._config["token_endpoint"], {
            "grant_type": DEVICE_GRANT, "client_id": self._config["client_id"],
            "device_code": device_code,
        }, min(self._timeout, remaining))

    def _wait(self, interval: float):
        until = min(self._clock() + interval, self._deadline)
        while self._clock() < until:
            self._sleep(until - self._clock())
        if self._clock() >= self._deadline:
            raise UserAuthError("user_auth_expired")


def _integer(value, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid duration")
    return value


def _verification_uri(value: str, origin: str) -> str:
    _text(value, 4096)
    parsed = urllib.parse.urlsplit(value)
    if not value.isascii() or "\\" in value or parsed.fragment or "#" in value:
        raise ValueError("invalid verification URI")
    clean = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if endpoint_origin(clean) != origin or not parsed.path.startswith("/"):
        raise ValueError("invalid verification origin")
    return value


def _challenge(value: dict, origin: str) -> dict:
    try:
        if "error" in value:
            raise ValueError("mixed success and error")
        code = _text(value["device_code"], 2048)
        user_code = _text(value["user_code"], 128)
        if not code.isascii() or not user_code.isascii():
            raise ValueError("invalid device code")
        result = {"device_code": code, "user_code": user_code,
                  "verification_uri": _verification_uri(value["verification_uri"], origin),
                  "expires_in": _integer(value["expires_in"], MAX_CHALLENGE_TTL),
                  "interval": _integer(value.get("interval", 5), 300)}
        if "verification_uri_complete" in value:
            result["verification_uri_complete"] = _verification_uri(value["verification_uri_complete"], origin)
        displayed = [user_code, result["verification_uri"], result.get("verification_uri_complete", "")]
        if any(code in text or code in urllib.parse.unquote(text) for text in displayed):
            raise ValueError("device credential appears in public challenge")
    except (KeyError, TypeError, ValueError):
        raise UserAuthError("invalid_user_auth_response") from None
    return result


def _poll_interval(value: dict, interval: float) -> float:
    error = value.get("error")
    if "access_token" in value or not isinstance(error, str):
        raise UserAuthError("invalid_user_auth_response", status=400)
    if error == "authorization_pending":
        return interval
    if error == "slow_down":
        return interval + 5
    if error == "access_denied":
        raise UserAuthError("user_auth_denied", status=400)
    if error in {"expired_token", "invalid_grant"}:
        raise UserAuthError("user_auth_expired", status=400)
    raise UserAuthError("user_auth_rejected", status=400)


def _credential(value: dict, scopes: list[str], wall: float, elapsed: float) -> dict:
    try:
        if "error" in value or not _valid_token(value["access_token"]):
            raise ValueError("invalid access token")
        ttl = _integer(value["expires_in"], MAX_TTL)
        if not isinstance(value["token_type"], str) or value["token_type"].lower() != "bearer":
            raise ValueError("invalid token type")
        if "scope" in value and not _matching_scope(value["scope"], scopes):
            raise ValueError("invalid token scopes")
        if elapsed >= ttl or not math.isfinite(wall) or wall <= 0:
            raise ValueError("expired access token")
    except (KeyError, TypeError, ValueError):
        raise UserAuthError("invalid_user_auth_response") from None
    return {"access_token": value["access_token"], "issued_at": float(wall), "expires_at": float(wall + ttl)}


__all__ = ["DeviceAuthorization", "UserAuthError"]
