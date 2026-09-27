"""OpenCode Go failure contract, independent of the CLI, scheduler and HTTP server.

Verified upstream contracts (anomalyco/opencode dev, 2026-09-21):
packages/console/app/src/routes/zen/util/error.ts
packages/console/app/src/routes/zen/util/handler.ts (catch/validateBilling)
GoUsageLimitError is a 429 error.type; metadata.limitName is top-level.
CreditsError/MonthlyLimitError/UserLimitError use 401 despite not being auth
failures. Only these exact provider error types authorize account failover.
Messages, including localized messages, never participate in classification.
This pure classifier never mutates balances. PoolStore records its decisions in
bounded audit history and persists quota windows before admitting another account.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import json
import math
import re
import time


MAX_ERROR_BYTES = 65536
MAX_RETRY_SECONDS = 35 * 86400
_WINDOWS = {"5 hour": "5h", "weekly": "weekly", "monthly": "monthly"}
_BALANCE_ERRORS = {"CreditsError": "balance", "MonthlyLimitError": "monthly",
                   "UserLimitError": "user"}


@dataclass(frozen=True)
class PoolFailure:
    kind: str
    code: str
    retry_at: float | None = None
    window: str = ""
    scope: str = "account"


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate provider error key")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError("invalid provider JSON number")


def _error_body(body: bytes) -> dict:
    if not isinstance(body, bytes) or len(body) > MAX_ERROR_BYTES:
        return {}
    try:
        data = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        return {}
    if not isinstance(data, dict) or data.get("type") != "error":
        return {}
    error = data.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("type"), str):
        return {}
    if not isinstance(error.get("message"), str):
        return {}
    return data


def _retry_at(headers: Mapping[str, str], now: float) -> float | None:
    values = [value for name, value in headers.items()
              if isinstance(name, str) and name.lower() == "retry-after"]
    if len(values) != 1 or not isinstance(values[0], str) or len(values[0]) > 128:
        return None
    value = values[0].strip()
    try:
        if re.fullmatch(r"[0-9]{1,8}", value):
            delay = int(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            delay = date.timestamp() - now
        if not math.isfinite(delay) or not 0 <= delay <= MAX_RETRY_SECONDS:
            return None
        return now + max(1, delay)
    except (ValueError, TypeError, OverflowError):
        return None


def classify_failure(status: int, headers: Mapping[str, str], body: bytes, *,
                     now: float | None = None) -> PoolFailure:
    """Classify trusted HTTP errors, never assistant text or arbitrary quota prose."""
    current = time.time() if now is None else now
    if type(current) not in (int, float) or not 0 <= current <= 10 ** 12:
        raise ValueError("now must be a finite Unix timestamp")
    retry_at = _retry_at(headers, current)
    data = _error_body(body)
    code = data.get("error", {}).get("type", "")
    if status == 429 and code == "GoUsageLimitError":
        metadata = data.get("metadata")
        limit = metadata.get("limitName") if isinstance(metadata, dict) else None
        window = _WINDOWS.get(limit, "unknown") if isinstance(limit, str) else "unknown"
        return PoolFailure("quota", code, retry_at, window)
    if status == 401 and code in _BALANCE_ERRORS:
        return PoolFailure("quota", code, retry_at, _BALANCE_ERRORS[code])
    if status in (401, 403) and code in ("ModelError", "RegionError", "DataPolicyError"):
        return PoolFailure("request", code)
    if status in (401, 403):
        return PoolFailure("auth", "authentication_failed")
    if status == 429:
        return PoolFailure("rate", "rate_limited", retry_at, "rate")
    if status >= 500 or 300 <= status < 400:
        return PoolFailure("service", "upstream_unavailable", retry_at)
    return PoolFailure("request", "upstream_rejected")
