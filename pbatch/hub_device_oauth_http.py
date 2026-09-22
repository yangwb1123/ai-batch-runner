"""Fixed-endpoint, bounded HTTP transport for public-client device login."""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.parse
import urllib.request

from .hub_client import HubError
from .hub_oauth_config import strict_json
from .hub_oauth_http import NoRedirect, TokenExchangeError, _read_response


class UserAuthError(HubError):
    """Public diagnostics never contain issuer payloads or credentials."""

    def __init__(self, code: str, *, status: int = 0, retryable: bool = False):
        messages = {
            "user_auth_unavailable": "user authorization service is unavailable",
            "user_auth_rejected": "user authorization request was rejected",
            "invalid_user_auth_response": "user authorization response is invalid",
            "user_auth_response_too_large": "user authorization response exceeds the limit",
            "user_auth_expired": "user authorization has expired",
            "user_auth_denied": "user authorization was denied",
            "invalid_user_auth_challenge": "user authorization challenge is unavailable or invalid",
        }
        super().__init__(messages[code], code=code, status=status)
        self.retryable = retryable


def new_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def post_form(opener, endpoint: str, values: dict, timeout: float) -> tuple[int, dict]:
    body = urllib.parse.urlencode(values).encode("ascii")
    request = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
    })
    deadline = time.monotonic() + timeout
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.status
            _check_status(status)
            raw = _read_response(response, deadline)
    except TokenExchangeError as error:
        _read_error(error)
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        raise UserAuthError("user_auth_unavailable", retryable=True) from None
    return status, _object(raw)


def _check_status(status: int):
    if status in {200, 400}:
        return
    if status == 429 or 500 <= status <= 599:
        raise UserAuthError("user_auth_unavailable", status=status, retryable=True)
    raise UserAuthError("user_auth_rejected", status=status)


def _read_error(error: TokenExchangeError):
    codes = {"token_response_too_large": "user_auth_response_too_large",
             "machine_auth_unavailable": "user_auth_unavailable"}
    code = codes.get(error.code, "invalid_user_auth_response")
    raise UserAuthError(code, retryable=error.transient) from None


def _object(raw: bytes) -> dict:
    try:
        value = strict_json(raw)
        if not isinstance(value, dict):
            raise ValueError("object required")
    except (ValueError, TypeError, RecursionError):
        raise UserAuthError("invalid_user_auth_response") from None
    return value
