"""Clock discipline helpers for local deadlines and control-plane leases.

Wall-clock timestamps are retained for audit and persistence.  Elapsed-time
and same-process lease expiry use a monotonic deadline whenever the lease was
issued by the current clock epoch; old or restarted records fall back to an
early-expiring wall-clock check with an explicit skew bound.
"""

from __future__ import annotations

import math
import time

DEFAULT_CLOCK_SKEW_SECONDS = 5.0


def _finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def deadline(timeout: float, now: float | None = None) -> float:
    """Return an absolute monotonic deadline for a non-negative timeout."""
    value = _finite(timeout, "timeout")
    if value < 0:
        raise ValueError("timeout must be >= 0")
    return (time.monotonic() if now is None else _finite(now, "now")) + value


def remaining(until: float, now: float | None = None) -> float:
    """Return non-negative seconds remaining on a monotonic deadline."""
    value = _finite(until, "until")
    current = time.monotonic() if now is None else _finite(now, "now")
    return max(0.0, value - current)


def lease_window(ttl_seconds: float, wall_now: float | None = None,
                 monotonic_now: float | None = None) -> dict:
    """Create audit and same-process expiry timestamps for one lease."""
    ttl = _finite(ttl_seconds, "ttl_seconds")
    if ttl < 0:
        raise ValueError("ttl_seconds must be >= 0")
    wall = time.time() if wall_now is None else _finite(wall_now, "wall_now")
    mono = (time.monotonic() if monotonic_now is None
            else _finite(monotonic_now, "monotonic_now"))
    return {"issued_at": wall, "expires_at": wall + ttl,
            "issued_monotonic": mono,
            "monotonic_expires_at": mono + ttl}


def lease_expired(lease: dict, *, clock_id: str = "",
                  wall_now: float | None = None,
                  monotonic_now: float | None = None,
                  skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS) -> bool:
    """Fail closed on expiry while avoiding wall-clock jumps in one process."""
    if not isinstance(lease, dict):
        return True
    skew = max(0.0, _finite(skew_seconds, "skew_seconds"))
    if (clock_id and lease.get("clock_id") == clock_id
            and lease.get("monotonic_expires_at") is not None):
        try:
            return (time.monotonic() if monotonic_now is None
                    else _finite(monotonic_now, "monotonic_now")) >= _finite(
                        lease["monotonic_expires_at"], "monotonic_expires_at")
        except (TypeError, ValueError):
            return True
    try:
        expires = _finite(lease.get("expires_at"), "expires_at")
        current = time.time() if wall_now is None else _finite(wall_now, "wall_now")
    except (TypeError, ValueError):
        return True
    # Expire slightly early when only wall time is available: a fast writer
    # must not exploit a slow reader's clock skew to submit stale evidence.
    return current >= expires - skew


__all__ = ["DEFAULT_CLOCK_SKEW_SECONDS", "deadline", "lease_expired",
           "lease_window", "remaining"]
