"""Provider rate limiting (F line / round-5 F).

A process-wide token bucket serializes agent invocations across parallel
workers, keyed by provider: `--min-interval` only throttles SUCCESSFUL
serial tasks, so a parallel batch (or a meta stage fan-out) can hammer one
provider and re-sync into a thundering herd on retry. A token bucket with
jittered retry waits spreads the load instead.

Config (pi-batch.yaml, `limits:` section) / CLI flags:

    limits:
      per_second: 0        # 0 = unlimited (default)
      burst: 1             # token bucket capacity
      providers: {}        # provider -> per_second override

    --rate-limit 2 --rate-burst 4

Semantics: each provider gets its own bucket; the "" bucket covers tasks
without a provider. acquire() blocks until a token is available — workers
wait in line instead of overloading the provider. When
PBATCH_RATE_LIMIT_FILE is configured, the same bucket is protected by a
cross-process file lock (Campaign sets this automatically for isolated
worktrees). Rate 0 (unlimited) returns immediately. The limiter is advisory,
never fatal: an unavailable shared state only makes the caller wait until its
normal acquire timeout.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

# module-level config, set by cli.main() from CLI flags / pi-batch.yaml
RATE_PER_SECOND: float = 0.0   # 0 = unlimited (default)
BURST: float = 1.0
PROVIDER_RATES: dict = {}      # provider name -> per_second override
SHARED_FILE: str = ""          # optional cross-process state file

_LOCK = threading.Lock()
# provider key -> [tokens, last_refill_epoch]
_BUCKETS: dict = {}


def configure(per_second: float = 0.0, burst: float = 1.0,
              providers: Optional[dict] = None, shared_file: str = "") -> None:
    """Reset the limiter from CLI/config values (called once at startup).
    Invalid values degrade to defaults; a negative rate disables the limiter."""
    global RATE_PER_SECOND, BURST, PROVIDER_RATES, SHARED_FILE
    with _LOCK:
        RATE_PER_SECOND = per_second if per_second > 0 else 0.0
        BURST = burst if burst > 0 else 1.0
        PROVIDER_RATES = {
            str(key): float(value) for key, value in (providers or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            and float(value) > 0
        }
        SHARED_FILE = str(shared_file or os.environ.get("PBATCH_RATE_LIMIT_FILE", "")).strip()
        _BUCKETS.clear()


def _rate_for(provider: str) -> float:
    """Effective per-second rate for a provider (override > global)."""
    if PROVIDER_RATES:
        rate = PROVIDER_RATES.get(provider or "")
        if rate is not None:
            return rate
    return RATE_PER_SECOND


def _refill(key: str, rate: float) -> None:
    """Add tokens earned since the last refill (bounded by burst)."""
    now = time.monotonic()
    tokens, last = _BUCKETS[key]
    _BUCKETS[key] = [min(BURST, tokens + (now - last) * rate), now]


def acquire(provider: str = "", timeout: Optional[float] = None) -> bool:
    """Block until a token is available for *provider* (or the optional
    timeout elapses, returning False). Unlimited (rate 0) -> True at once.
    Polling with a short sleep keeps the implementation dependency-free and
    lets parallel workers line up fairly."""
    rate = _rate_for(provider)
    if rate <= 0:
        return True
    key = provider or ""
    deadline = None if timeout is None else time.monotonic() + timeout
    shared = _shared_path()
    while True:
        if shared is not None:
            acquired, wait = _acquire_shared(shared, key, rate)
            if acquired:
                return True
        else:
            wait = _acquire_local(key, rate)
            if wait is None:
                return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(min(max(wait, 0.01), 0.25))


def _acquire_local(key: str, rate: float) -> Optional[float]:
    """Consume a local token; return the seconds until the next token."""
    with _LOCK:
        if key not in _BUCKETS:
            _BUCKETS[key] = [BURST, time.monotonic()]
        tokens, _ = _BUCKETS[key]
        if tokens >= 1.0:
            _BUCKETS[key][0] = tokens - 1.0
            return None
        _refill(key, rate)
        tokens = _BUCKETS[key][0]
        if tokens >= 1.0:
            _BUCKETS[key][0] = tokens - 1.0
            return None
        return (1.0 - tokens) / rate


def _shared_path() -> Optional[Path]:
    value = SHARED_FILE or os.environ.get("PBATCH_RATE_LIMIT_FILE", "")
    if not value:
        return None
    return Path(value).expanduser()


def _acquire_shared(path: Path, key: str, rate: float) -> tuple[bool, float]:
    """Atomically consume a token shared by independent runner processes."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            return False, 0.25
    except OSError:
        return False, 0.25
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            return False, 0.25
        return False, 0.25
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = _read_shared_state(handle)
            now = time.time()
            entry = state.get(key, {})
            previous, last = _shared_values(entry, now)
            tokens = min(BURST, max(0.0, previous) + max(0.0, now - last) * rate)
            if tokens >= 1.0:
                state[key] = {"tokens": tokens - 1.0, "last": now}
                _write_shared_state(handle, state)
                return True, 0.0
            state[key] = {"tokens": tokens, "last": now}
            _write_shared_state(handle, state)
            return False, (1.0 - tokens) / rate
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _shared_values(entry, now: float) -> tuple[float, float]:
    if not isinstance(entry, dict):
        return BURST, now
    try:
        previous = float(entry.get("tokens", BURST))
        last = float(entry.get("last", now))
        if not (0 <= previous == previous < float("inf")):
            raise ValueError
        if not (last == last and abs(last) < float("inf")):
            raise ValueError
        return previous, last
    except (TypeError, ValueError, OverflowError):
        return BURST, now


def _read_shared_state(handle) -> dict:
    handle.seek(0)
    raw = handle.read(64 * 1024)
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_shared_state(handle, state: dict) -> None:
    payload = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    handle.seek(0)
    handle.truncate()
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())
