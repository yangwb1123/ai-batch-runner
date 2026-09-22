"""The bounded client_credentials exchange used by the machine provider."""

from __future__ import annotations

import base64
import http.client
import time
import urllib.error
import urllib.parse
import urllib.request

from .hub_client import HubError
from .hub_oauth_config import read_secret, strict_json

MAX_RESPONSE_BYTES = 32 * 1024
MAX_ACCESS_TOKEN_BYTES = 16 * 1024
MAX_TTL = 86400


class TokenExchangeError(HubError):
    """Only fixed diagnostics cross the credential transport boundary."""

    def __init__(self, code: str, *, status: int = 0, transient: bool = False):
        messages = {
            "credential_unavailable": "private machine credential is unavailable",
            "machine_auth_unavailable": "machine authorization service is unavailable",
            "machine_auth_rejected": "machine authorization request was rejected",
            "invalid_token_response": "machine authorization response is invalid",
            "token_response_too_large": "machine authorization response exceeds the limit",
        }
        super().__init__(messages[code], code=code, status=status)
        self.transient = transient


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def token_request(config: dict) -> urllib.request.Request:
    try:
        secret = read_secret(config["client_secret_file"])
    except (OSError, ValueError):
        raise TokenExchangeError("credential_unavailable", status=401) from None
    basic = base64.b64encode((config["client_id"] + ":" + secret).encode("utf-8")).decode("ascii")
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "resource": config["resource"],
                                   "scope": " ".join(config["scopes"])}).encode("ascii")
    return urllib.request.Request(config["token_endpoint"], data=body, method="POST", headers={
        "Authorization": "Basic " + basic, "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    })


def exchange(config: dict, opener, timeout: float) -> tuple[str, int]:
    request = token_request(config)
    deadline = time.monotonic() + timeout
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                _reject_status(response.status)
            raw = _read_response(response, deadline)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        _reject_status(status)
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        raise TokenExchangeError("machine_auth_unavailable", transient=True) from None
    return _decode_token(raw, config["scopes"])


def _reject_status(status: int):
    transient = status == 429 or 500 <= status <= 599
    code = "machine_auth_unavailable" if transient else "machine_auth_rejected"
    raise TokenExchangeError(code, status=status, transient=transient) from None


def _read_response(response, deadline: float) -> bytes:
    if response.headers.get_content_type() != "application/json":
        raise TokenExchangeError("invalid_token_response")
    length = response.headers.get("Content-Length", "")
    if length.isdigit() and (len(length) > 10 or int(length) > MAX_RESPONSE_BYTES):
        raise TokenExchangeError("token_response_too_large")
    chunks, size = [], 0
    while size <= MAX_RESPONSE_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TokenExchangeError("machine_auth_unavailable", transient=True)
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - size))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
    raise TokenExchangeError("token_response_too_large")


def _decode_token(raw: bytes, scopes: list[str]) -> tuple[str, int]:
    try:
        value = strict_json(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid object")
        if "error" in value:
            raise ValueError("mixed success and error")
        token, ttl = value["access_token"], value["expires_in"]
        if not _valid_token(token) or type(ttl) is not int or not 1 <= ttl <= MAX_TTL:
            raise ValueError("invalid credential")
        if not isinstance(value["token_type"], str) or value["token_type"].lower() != "bearer":
            raise ValueError("invalid type")
        if "scope" in value and not _matching_scope(value["scope"], scopes):
            raise ValueError("invalid scope")
    except (KeyError, TypeError, ValueError, RecursionError):
        raise TokenExchangeError("invalid_token_response") from None
    return token, ttl


def _valid_token(token) -> bool:
    return (isinstance(token, str) and 0 < len(token) <= MAX_ACCESS_TOKEN_BYTES
            and all(33 <= ord(char) <= 126 for char in token))


def _matching_scope(value, requested: list[str]) -> bool:
    if not isinstance(value, str):
        return False
    actual = value.split(" ")
    return len(actual) == len(requested) and set(actual) == set(requested)
