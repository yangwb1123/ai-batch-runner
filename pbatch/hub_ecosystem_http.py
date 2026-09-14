"""Bounded JSON transport shared by the three opt-in ecosystem adapters."""

from __future__ import annotations

import http.client
import json
import math
import os
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

from .hub_client import validate_hub_url


class IntegrationError(RuntimeError):
    """Safe diagnostics: never retain an upstream response or credentials."""

    def __init__(self, code: str, *, status: int = 0, retryable: bool = True):
        super().__init__(code)
        self.code, self.status, self.retryable = code, status, retryable


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_token(path: str) -> str:
    try:
        fd = os.open(os.path.expanduser(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.getuid()):
                raise ValueError("private token permissions")
            raw = stream.read(16385)
        token = raw.decode("utf-8").strip()
        if (not token or not token.isascii() or len(raw) > 16384
                or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in token)):
            raise ValueError("private token format")
        return token
    except (OSError, ValueError, UnicodeError):
        raise IntegrationError("credential_unavailable") from None


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _decode(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                          parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise IntegrationError("invalid_response", retryable=False) from None


def _constant(value):
    raise ValueError("non-finite JSON number")


def _http_failure(exc, deadline, retry_codes):
    status = exc.code
    retryable = status in {401, 403, 408, 425, 429} or status >= 500
    try:
        if status == 409 and retry_codes:
            body = _read(exc, 16384, deadline)
            if isinstance(body, dict):
                error = body.get("error", body)
                if isinstance(error, dict) and error.get("code") in retry_codes:
                    retryable = True
    except (IntegrationError, OSError, http.client.HTTPException):
        pass  # A malformed error response cannot turn a permanent error into a retry.
    finally:
        exc.close()
    return IntegrationError("http_error", status=status, retryable=retryable)


def _read(response, maximum, deadline):
    pieces, size = [], 0
    while size <= maximum:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IntegrationError("transport_timeout")
        # urllib's HTTPResponse is buffered; read1 performs at most one socket read.
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = response.read1(min(8192, maximum + 1 - size))
        if not chunk:
            return _decode(b"".join(pieces))
        pieces.append(chunk)
        size += len(chunk)
    raise IntegrationError("response_too_large", retryable=False)


class JSONTransport:
    def __init__(self, base_url: str, token_file: str, *, timeout=5.0,
                 ca_file="", cert_file="", key_file="", max_response_bytes=262144):
        self.base_url = validate_hub_url(base_url)
        if not isinstance(token_file, str) or not token_file:
            raise ValueError("integration token_file is required")
        if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
                or not math.isfinite(timeout) or not 0 < timeout <= 30):
            raise ValueError("integration timeout must be within (0,30]")
        if (isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int)
                or not 1024 <= max_response_bytes <= 1048576):
            raise ValueError("integration response limit must be 1 KiB..1 MiB")
        if bool(cert_file) != bool(key_file):
            raise ValueError("integration mTLS requires cert_file and key_file")
        context = ssl.create_default_context(cafile=ca_file or None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if cert_file:
            context.load_cert_chain(cert_file, key_file)
        self.token_file, self.timeout = token_file, float(timeout)
        self.maximum = max_response_bytes
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=context))

    def request(self, method: str, path: str, *, body=None, headers=None, expected=(200,), retry_codes=()):
        parsed = urllib.parse.urlsplit(path)
        if not path.startswith("/") or path.startswith("//") or parsed.netloc or parsed.fragment:
            raise ValueError("integration path must be relative to its configured origin")
        supplied = dict(headers or {})
        if any(name.lower() in {"authorization", "host", "content-length"} for name in supplied):
            raise ValueError("integration credential/transport headers cannot be overridden")
        data = canonical(body).encode("utf-8") if body is not None else None
        if data is not None and len(data) > 65536:
            raise ValueError("integration request exceeds 64 KiB")
        supplied.update(Authorization="Bearer " + private_token(self.token_file),
                        Accept="application/json")
        if data is not None:
            supplied["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data,
                                         headers=supplied, method=method)
        deadline = time.monotonic() + self.timeout
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status not in expected:
                    raise IntegrationError("unexpected_status", status=response.status)
                if response.headers.get_content_type() != "application/json":
                    raise IntegrationError("invalid_content_type", retryable=False)
                return _read(response, self.maximum, deadline)
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc, deadline, retry_codes) from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise IntegrationError("transport_failed") from None
