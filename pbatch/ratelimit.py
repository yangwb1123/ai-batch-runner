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
from . import file_lock as fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
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
_SHARED_VERSION = 2
_SHARED_MAX_BYTES = 64 * 1024
_SHARED_MAX_BUCKETS = 128
_SHARED_OVERFLOW_KEY = "overflow"
_SHARED_KEY_PREFIX = "provider:"


def configure(per_second: float = 0.0, burst: float = 1.0,
              providers: Optional[dict] = None,
              shared_file: Optional[str] = None) -> None:
    """Reset the limiter from CLI/config values (called once at startup).
    Invalid values degrade to defaults; a negative rate disables the limiter.
    ``None`` discovers PBATCH_RATE_LIMIT_FILE, while an explicit empty string
    selects a process-local bucket even when the environment is populated."""
    global RATE_PER_SECOND, BURST, PROVIDER_RATES, SHARED_FILE
    with _LOCK:
        RATE_PER_SECOND = _finite_number(per_second, 0.0, minimum=0.0)
        BURST = _finite_number(burst, 1.0, minimum=0.0)
        if BURST <= 0:
            BURST = 1.0
        PROVIDER_RATES = _provider_rates(providers)
        selected = (os.environ.get("PBATCH_RATE_LIMIT_FILE", "")
                    if shared_file is None else shared_file)
        SHARED_FILE = str(selected or "").strip()
        _BUCKETS.clear()


def _finite_number(value, default: float, minimum: float) -> float:
    """Return a finite value within the accepted range, else *default*."""
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) and number >= minimum else default


def _provider_rates(providers: Optional[dict]) -> dict[str, float]:
    """Keep finite non-negative overrides; zero explicitly means unlimited."""
    if not isinstance(providers, dict):
        return {}
    values: dict[str, float] = {}
    for key, value in providers.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and number >= 0:
            values[str(key)] = number
    return values


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


def acquire(provider: str = "", timeout: Optional[float] = None,
            cancel_event=None) -> bool:
    """Block until a token is available or a scoped cancellation is requested.
    Unlimited (rate 0) -> True at once. Polling with a short sleep keeps the
    implementation dependency-free and lets parallel workers line up fairly."""
    if _cancel_requested(cancel_event):
        return False
    rate = _rate_for(provider)
    if rate <= 0:
        return True
    key = provider or ""
    deadline = _deadline(timeout)
    if timeout is not None and deadline is None:
        return False
    shared = _shared_path()
    while True:
        if _cancel_requested(cancel_event):
            return False
        if shared is not None:
            acquired, wait = _acquire_shared(shared, key, rate)
            if acquired:
                return True
        else:
            wait = _acquire_local(key, rate)
            if wait is None:
                return True
        delay = min(max(wait, 0.01), 0.25)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            delay = min(delay, remaining)
        if cancel_event is not None:
            try:
                if cancel_event.wait(delay):
                    return False
            except (AttributeError, TypeError):
                time.sleep(delay)
        else:
            time.sleep(delay)


def _cancel_requested(cancel_event) -> bool:
    try:
        return bool(cancel_event is not None and cancel_event.is_set())
    except (AttributeError, TypeError):
        return False


def _deadline(timeout: Optional[float]) -> Optional[float]:
    if timeout is None:
        return None
    seconds = _finite_number(timeout, -1.0, minimum=0.0)
    return time.monotonic() + seconds if seconds >= 0 else None


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
    # configure() resolves environment/default precedence once. Re-reading
    # os.environ here would override an explicit process-local selection and
    # let unrelated runtime wiring silently change an active limiter.
    if not SHARED_FILE:
        return None
    return Path(SHARED_FILE).expanduser()


def _acquire_shared(path: Path, key: str, rate: float) -> tuple[bool, float]:
    """Atomically consume a token shared by independent runner processes."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _open_shared_lock(path) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                state = _read_shared_path(path)
                if state is None:
                    return False, 0.25
                result = _consume_shared_bucket(state, key, rate)
                _write_shared_path(path, state)
                return result
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False, 0.25


def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open a shared-state inode without blocking on special files."""
    descriptor = os.open(
        path, flags | getattr(os, "O_NONBLOCK", 0), mode)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"rate-limit path is not a regular file: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_shared_lock(path: Path):
    lock_path = path.with_name(f"{path.name}.lock")
    if lock_path.is_symlink():
        raise OSError("rate-limit lock is a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = _open_regular(lock_path, flags)
    return os.fdopen(descriptor, "r+b", buffering=0)


def _read_shared_path(path: Path) -> Optional[dict]:
    if path.is_symlink():
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = _open_regular(path, flags)
    except FileNotFoundError:
        return {"version": _SHARED_VERSION, "buckets": {}}
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            return None
        raise
    with os.fdopen(descriptor, "rb") as handle:
        return _read_shared_state(handle)


def _write_shared_path(path: Path, state: dict) -> None:
    payload = _shared_payload(state)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _consume_shared_bucket(state: dict, provider: str,
                           rate: float) -> tuple[bool, float]:
    now = time.time()
    buckets = state["buckets"]
    key = _shared_bucket_key(
        buckets, _provider_state_key(provider), now, rate)
    entry = buckets.get(key)
    effective_rate = (_overflow_rate(entry, rate)
                      if key == _SHARED_OVERFLOW_KEY else rate)
    values = _shared_values(entry)
    if values is None:
        raise OSError("invalid shared rate-limit bucket")
    previous, last = values
    tokens = min(
        BURST, previous + max(0.0, now - last) * effective_rate)
    if tokens >= 1.0:
        buckets[key] = _shared_entry(tokens - 1.0, now, effective_rate)
        return True, 0.0
    buckets[key] = _shared_entry(tokens, now, effective_rate)
    wait = ((1.0 - tokens) / effective_rate
            if effective_rate > 0 else 0.25)
    return False, wait


def _provider_state_key(provider: object) -> str:
    raw = str(provider or "").encode("utf-8", errors="surrogatepass")
    return f"{_SHARED_KEY_PREFIX}{hashlib.sha256(raw).hexdigest()}"


def _shared_bucket_key(buckets: dict, key: str, now: float,
                       rate: float) -> str:
    if key in buckets:
        return key
    if _SHARED_OVERFLOW_KEY in buckets:
        return _SHARED_OVERFLOW_KEY
    if len(buckets) < _SHARED_MAX_BUCKETS:
        buckets[key] = _shared_entry(BURST, now, rate)
        return key
    victim = min(buckets, key=lambda item: (
        _shared_values(buckets[item]) or (0.0, now))[0])
    buckets[_SHARED_OVERFLOW_KEY] = buckets.pop(victim)
    return _SHARED_OVERFLOW_KEY


def _shared_entry(tokens: float, last: float,
                  rate: Optional[float]) -> dict[str, float]:
    entry = {"tokens": tokens, "last": last}
    if rate is not None:
        entry["rate"] = rate
    return entry


def _stored_rate(entry: object) -> Optional[float]:
    if not isinstance(entry, dict) or "rate" not in entry:
        return None
    try:
        rate = float(entry["rate"])
    except (TypeError, ValueError, OverflowError):
        return None
    return rate if math.isfinite(rate) and rate >= 0 else None


def _overflow_rate(entry: dict, current: float) -> float:
    """Persist the minimum rate of every identity sharing overflow.

    A lower-rate identity invalidates leftover tokens minted by a faster
    member. Legacy overflow entries have no trustworthy rate, so their
    leftovers are discarded and they do not refill until explicitly reset.
    """
    stored = _stored_rate(entry)
    effective = min(stored, current) if stored is not None else 0.0
    if stored is None or effective < stored:
        entry["tokens"] = 0.0
    return effective


def _shared_values(entry) -> Optional[tuple[float, float]]:
    if not isinstance(entry, dict):
        return None
    try:
        previous = float(entry["tokens"])
        last = float(entry["last"])
        if not (0 <= previous == previous < float("inf")):
            raise ValueError
        if not (last == last and abs(last) < float("inf")):
            raise ValueError
        return previous, last
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _read_shared_state(handle) -> Optional[dict]:
    handle.seek(0)
    raw = handle.read(_SHARED_MAX_BYTES + 1)
    if not raw:
        return None
    if len(raw) > _SHARED_MAX_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return _normalize_shared_state(value)


def _normalize_shared_state(value: object) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    versioned = value.get("version") == _SHARED_VERSION
    raw = value.get("buckets") if versioned else value
    if not isinstance(raw, dict) or len(raw) > _SHARED_MAX_BUCKETS:
        return None
    buckets: dict[str, dict[str, float]] = {}
    for raw_key, entry in raw.items():
        key = str(raw_key)
        if versioned:
            if key != _SHARED_OVERFLOW_KEY and not _valid_state_key(key):
                return None
        else:
            key = _provider_state_key(key)
        values = _shared_values(entry)
        if values is None:
            return None
        rate = _validated_stored_rate(entry)
        if rate is False:
            return None
        previous = buckets.get(key)
        buckets[key] = _conservative_bucket(previous, values, rate)
    return {"version": _SHARED_VERSION, "buckets": buckets}


def _valid_state_key(key: str) -> bool:
    digest = key.removeprefix(_SHARED_KEY_PREFIX)
    return (key.startswith(_SHARED_KEY_PREFIX) and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest))


def _conservative_bucket(previous: Optional[dict],
                         values: tuple[float, float],
                         rate: Optional[float]) -> dict[str, float]:
    if previous is None:
        return _shared_entry(values[0], values[1], rate)
    current = _shared_values(previous)
    if current is None:
        return _shared_entry(values[0], values[1], rate)
    previous_rate = _stored_rate(previous)
    rates = [item for item in (previous_rate, rate) if item is not None]
    merged_rate = min(rates) if len(rates) == 2 else None
    return _shared_entry(
        min(current[0], values[0]), max(current[1], values[1]), merged_rate)


def _validated_stored_rate(entry: object):
    if not isinstance(entry, dict) or "rate" not in entry:
        return None
    rate = _stored_rate(entry)
    return rate if rate is not None else False


def _shared_payload(state: dict) -> bytes:
    payload = json.dumps(
        state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(payload) > _SHARED_MAX_BYTES:
        raise OSError("shared rate-limit state exceeds size limit")
    return payload
