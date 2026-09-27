"""Verified Go usage contract and bounded, authenticated read-only queries.

Source: anomalyco/opencode packages/console/app/src/routes/zen/go/v1/usage.ts
(2026-09-21). Percent is *used*, not remaining. Rolling/weekly/monthly are all
required; uncertain or stale observations never authorize strict admission.
No subscription mutation, balance purchase, redirects or raw error persistence.
"""

from __future__ import annotations

from datetime import datetime
import http.client
import json
import math
import time

from .llm_pool_config import GO_ORIGIN
from .llm_pool_go import MAX_ERROR_BYTES, MAX_RETRY_SECONDS, _invalid_constant, _unique_object
from .llm_pool_transport import UpstreamRequest


class UsageQueryError(ValueError):
    """A fixed public error code, never a provider message or credential."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _window(value, now: float) -> dict:
    if not isinstance(value, dict) or set(value) != {"status", "percent", "resetsAt"}:
        raise ValueError("invalid usage window")
    status, percent, reset = value["status"], value["percent"], value["resetsAt"]
    if (status not in ("ok", "rate-limited") or type(percent) is not int
            or not 0 <= percent <= 100 or (status == "rate-limited") != (percent == 100)):
        raise ValueError("invalid usage status or percent")
    if not isinstance(reset, str) or not 20 <= len(reset) <= 40:
        raise ValueError("invalid usage reset time")
    date = datetime.fromisoformat(reset.removesuffix("Z") + ("+00:00" if reset.endswith("Z") else ""))
    if date.tzinfo is None:
        raise ValueError("usage reset time requires a timezone")
    timestamp = date.timestamp()
    if not now < timestamp <= now + MAX_RETRY_SECONDS:
        raise ValueError("expired or implausible usage reset time")
    return {"status": status, "percent": percent, "resets_at": timestamp}


def parse_usage(body: bytes, *, now: float | None = None) -> dict:
    current = time.time() if now is None else now
    if type(current) not in (int, float) or not 0 <= current <= 10 ** 12:
        raise ValueError("now must be a finite Unix timestamp")
    if not isinstance(body, bytes) or len(body) > MAX_ERROR_BYTES:
        raise ValueError("usage response exceeds limit")
    data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=_invalid_constant)
    if not isinstance(data, dict) or set(data) != {"usage"}:
        raise ValueError("usage response envelope required")
    usage = data["usage"]
    if not isinstance(usage, dict) or set(usage) != {"rolling", "weekly", "monthly"}:
        raise ValueError("three usage windows required")
    return {("5h" if name == "rolling" else name): _window(value, current)
            for name, value in usage.items()}


def _read_usage(response, request) -> dict:
    if response.status != 200:
        error = {401: "auth", 403: "auth", 429: "rate"}.get(response.status, "service")
        raise UsageQueryError(error)
    if (response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            != "application/json" or response.getheader("Content-Encoding", "identity") != "identity"):
        raise UsageQueryError("invalid_response")
    lengths = response.headers.get_all("Content-Length", [])
    if lengths and (len(lengths) != 1 or not lengths[0].isdigit()
                    or len(lengths[0]) > 6 or int(lengths[0]) > MAX_ERROR_BYTES):
        raise UsageQueryError("invalid_response")
    raw = response.read(MAX_ERROR_BYTES + 1)
    request.remaining()
    if lengths and len(raw) != int(lengths[0]):
        raise UsageQueryError("invalid_response")
    return parse_usage(raw)


def fetch_usage(key: str, timeout: float, *, upstream_url: str = GO_ORIGIN) -> dict:
    """One GET, no retry. Fixed origin except explicit loopback test servers."""
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 30):
        raise ValueError("usage timeout must be in (0,30]")
    try:
        with UpstreamRequest(upstream_url, timeout) as request:
            response = request.usage(key)
            try:
                return _read_usage(response, request)
            finally:
                response.close()
    except UsageQueryError:
        raise
    except (OSError, http.client.HTTPException):
        raise UsageQueryError("transport") from None
    except (ValueError, OverflowError, RecursionError):
        raise UsageQueryError("invalid_response") from None
