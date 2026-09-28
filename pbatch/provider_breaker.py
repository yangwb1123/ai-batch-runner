"""Provider-level circuit breaker（T17a，retro 实战沉淀）。

aero-campaign 复盘（retro --failures-only logs/）暴露真实缺口：agent
失败签名（quota/rate-limit/auth/network）跨任务连续命中 498 次，而
per-task Circuit 与 round 级 StallDetector 都看不见（每任务只失败一
次就换任务）——7×24 无人值守在 provider 系统性故障时会空转烧钱。

本模块：连续 N 次 provider 失败（不同任务也算）→ 进入指数退避冷却；
冷却期间同一 provider 的任务 spawn 前等待（有界，超限则以明确 reason
失败而非假装执行）；同一 provider 任一成功重置其窗口，不影响其他
provider。事件进交互账本、日志清晰可见（retro 可复盘 provider_breaker 行）。

配置（pi-batch.yaml limits.provider_breaker）：
  threshold     连续 provider 失败数（默认 5；0 = 关闭）
  base_cooldown 首次冷却秒数（默认 60，指数退避 ×2）
  max_cooldown  单段冷却上限（默认 900s；超出后任务直接失败）
"""

from __future__ import annotations

from . import file_lock as fcntl
import hashlib
import json
from email.utils import parsedate_to_datetime
import math
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from . import config
from .config import log

_DEFAULT_THRESHOLD = 5
_DEFAULT_BASE = 60
_DEFAULT_MAX = 900

_lock = threading.RLock()
_STATE_NAME = "provider-breaker.json"
_STATE_VERSION = 2
_STATE_MAX_BYTES = 64 * 1024
_MAX_PROVIDER_ENTRIES = 128
_OVERFLOW_PROVIDER = "__overflow__"
_ENTRY_FIELDS = ("streak", "step", "cooldown_until", "updated_at")
_PROVIDER_FAILURE_PREFIXES = (
    "agent reported provider failure (",
    "agent replied with a provider failure (",
)
_QUOTA_EXHAUSTION_RE = re.compile(
    r"go_?usage_?limit_?error|usage_?limit_?exceeded|insufficient_?quota|"
    r"quota_?exceeded|credit_?balance_?too_?low|insufficient\s+(?:balance|funds)",
    re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(
    r"(?im)^\s*(?:retry-after|x-ratelimit-reset|ratelimit-reset|reset_at|reset)"
    r"\s*[:=]\s*(.*?)\s*$")
_MAX_RETRY_AFTER = 7 * 24 * 60 * 60


def _state_path(data_dir=None) -> str:
    """返回项目数据目录内的共享状态文件；显式目录优先于 env。"""
    if data_dir is not None:
        return str(Path(data_dir) / _STATE_NAME)
    override = os.environ.get("PBATCH_PROVIDER_BREAKER_FILE", "").strip()
    if override:
        return override
    return str(Path(config.data_dir()) / _STATE_NAME)


def _read_handle(handle) -> dict:
    handle.seek(0)
    raw = handle.read(_STATE_MAX_BYTES + 1)
    if not raw:
        # Missing state is handled before opening the file. An existing but
        # empty file is a torn legacy write and must not silently reset an
        # active circuit.
        return _error_state("empty")
    if len(raw.encode("utf-8", errors="replace")) > _STATE_MAX_BYTES:
        return _error_state("oversized")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return _error_state("malformed")
    return value if isinstance(value, dict) else _error_state("malformed")


def _state_payload(state: dict) -> str:
    state["version"] = _STATE_VERSION
    state["clock"] = "unix"
    payload = json.dumps(state, separators=(",", ":"),
                         sort_keys=True)
    if len(payload.encode("utf-8")) > _STATE_MAX_BYTES:
        raise ValueError("provider breaker state exceeds size limit")
    return payload


def _lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open a state inode without blocking on FIFOs/devices."""
    descriptor = os.open(
        path, flags | getattr(os, "O_NONBLOCK", 0), mode)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"shared state path is not a regular file: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_lock(path: Path):
    lock_path = _lock_path(path)
    if lock_path.is_symlink():
        raise OSError("provider breaker lock is a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = _open_regular(lock_path, flags)
    return os.fdopen(descriptor, "r+", encoding="utf-8")


def _read_path(path: Path) -> dict:
    if path.is_symlink():
        return _error_state("symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = _open_regular(path, flags)
    except FileNotFoundError:
        return {}
    except OSError:
        return _error_state("unreadable")
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        return _read_handle(handle)


def _write_path(path: Path, state: dict) -> None:
    """Atomically publish a complete state while the stable lock is held."""
    payload = _state_payload(state)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        # Windows inherits the runtime directory's ACL; mode 0600 is not
        # an equivalent Windows access-control guarantee.
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        _sync_parent(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _sync_parent(path: Path) -> None:
    # Windows cannot open a directory with CRT os.open for directory fsync.
    # The temporary file is fsynced above on both platforms.
    if os.name == "nt":
        return
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_state(data_dir=None) -> dict:
    path = Path(_state_path(data_dir))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _open_lock(path) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return _read_path(path)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        return _error_state("unreadable")


def _mutate_state(mutator, data_dir=None, allow_repair: bool = False):
    """原子地锁住共享文件并执行一次 read-modify-write。"""
    path = Path(_state_path(data_dir))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _open_lock(path) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                state = _read_path(path)
                if state.get("state_error") and not allow_repair:
                    return None
                result = mutator(state)
                _write_path(path, state)
                return result
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        return None  # unavailable state must not crash an agent run


def _now() -> float:
    """Persistent deadlines use the Unix clock across process reboots."""
    return time.time()


def _error_state(reason: str) -> dict:
    """Represent unreadable state as an aggregate fail-closed partition."""
    return {
        "version": _STATE_VERSION,
        "clock": "unix",
        "state_error": reason,
        "providers": {_OVERFLOW_PROVIDER: {
            "streak": 0, "step": 1,
            "cooldown_until": _now() + _max_cooldown(),
            "updated_at": _now(),
        }},
    }


def _settings() -> dict:
    limits = config._section("limits")
    section = limits.get("provider_breaker")
    return section if isinstance(section, dict) else {}


def threshold() -> int:
    value = _settings().get("threshold", _DEFAULT_THRESHOLD)
    try:
        if isinstance(value, bool):
            raise ValueError
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_THRESHOLD
    return number if number >= 0 else 0


def _base_cooldown() -> float:
    value = _settings().get("base_cooldown", _DEFAULT_BASE)
    try:
        number = float(value)
        return max(0.0, number) if math.isfinite(number) else float(_DEFAULT_BASE)
    except (TypeError, ValueError, OverflowError):
        return float(_DEFAULT_BASE)


def _max_cooldown() -> float:
    value = _settings().get("max_cooldown", _DEFAULT_MAX)
    try:
        number = float(value)
        return max(1.0, number) if math.isfinite(number) else float(_DEFAULT_MAX)
    except (TypeError, ValueError, OverflowError):
        return float(_DEFAULT_MAX)


def _quota_cooldown() -> float:
    value = _settings().get("quota_cooldown", 24 * 60 * 60)
    try:
        number = float(value)
        return min(_MAX_RETRY_AFTER, max(1.0, number)) if math.isfinite(number) else 86400.0
    except (TypeError, ValueError, OverflowError):
        return 86400.0


def enabled() -> bool:
    return threshold() > 0


def _state_int(state: dict, key: str) -> int:
    try:
        return max(0, int(state.get(key, 0) or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _state_float(state: dict, key: str) -> float:
    try:
        value = float(state.get(key, 0.0) or 0.0)
        return value if math.isfinite(value) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _streak_after(streak: int, limit: int) -> int:
    return 0 if streak >= limit else streak


def is_provider_failure_reason(reason: str) -> bool:
    """Return whether a runner rejection represents provider health."""
    return str(reason or "").startswith(_PROVIDER_FAILURE_PREFIXES)


def is_quota_exhaustion(text: str) -> bool:
    """Distinguish exhausted account/provider quota from transient 429s."""
    return bool(_QUOTA_EXHAUSTION_RE.search(str(text or "")))


def retry_after_seconds(text: str, *, now: float | None = None) -> float:
    """Read bounded Retry-After/reset metadata from provider diagnostics."""
    current = _now() if now is None else float(now)
    delays = []
    for match in _RETRY_AFTER_RE.finditer(str(text or "")[:64 * 1024]):
        value = match.group(1).strip().strip("\"'")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = parsedate_to_datetime(value)
                number = parsed.timestamp() - current
            except (TypeError, ValueError, OverflowError):
                try:
                    from datetime import datetime
                    number = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() - current
                except (TypeError, ValueError, OverflowError):
                    continue
            else:
                number -= current
        if number >= 1_000_000_000:
            number -= current
        if math.isfinite(number) and number > 0:
            delays.append(min(number, _MAX_RETRY_AFTER))
    return max(delays, default=0.0)


def provider_failure_reason(stderr: str, stdout: str = "",
                            bin_name: str = "") -> str:
    """Classify provider health independently from artifact rejection."""
    candidates = [str(stderr or "")]
    lines = [line for line in str(stdout or "").splitlines() if line.strip()]
    if lines and len(lines) <= 8:
        candidates.append("\n".join(lines))
    for pattern in config.agent_provider_error_patterns(bin_name):
        if any(pattern.search(value) for value in candidates if value):
            return f"agent reported provider failure ({pattern.pattern})"
    return ""


def _bounded_identity(kind: str, value: object) -> str:
    text = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")[:120]
    if safe != text:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        safe = f"{safe or 'default'}-{digest}"
    return f"{kind}:{safe or 'default'}"


def breaker_identity(provider: str = "", agent: str = "") -> str:
    """Stable breaker partition: explicit provider, otherwise agent binary."""
    provider = str(provider or "").strip()
    if provider:
        return _bounded_identity("provider", provider)
    return _bounded_identity("agent", Path(str(agent or config.AGENT_BIN)).name)


def provider_identity(task) -> str:
    """Resolve a task onto the provider/agent breaker partition."""
    try:
        agent = task.effective_agent()
    except (AttributeError, TypeError, ValueError):
        agent = config.AGENT_BIN
    return breaker_identity(getattr(task, "provider", ""), agent)


def _deadline(value: object, clock: str) -> float:
    deadline = _state_float({"value": value}, "value")
    if deadline <= 0 or clock == "unix" or deadline >= 1_000_000_000:
        return deadline
    remaining = max(0.0, deadline - time.monotonic())
    return _now() + min(remaining, _max_cooldown()) if remaining else 0.0


def _clean_entry(value: object, clock: str = "unix") -> dict:
    entry = value if isinstance(value, dict) else {}
    return {
        "streak": _state_int(entry, "streak"),
        "step": _state_int(entry, "step"),
        "cooldown_until": _deadline(entry.get("cooldown_until"), clock),
        "updated_at": _state_float(entry, "updated_at") or _now(),
    }


def _has_legacy_entry(state: dict) -> bool:
    return any(field in state for field in _ENTRY_FIELDS)


def _provider_entries(state: dict) -> dict[str, dict]:
    raw = state.get("providers")
    if isinstance(raw, dict):
        clock = str(state.get("clock", ""))
        entries = {str(key): _clean_entry(value, clock)
                   for key, value in raw.items() if str(key)}
        return _bounded_entries(entries)
    if _has_legacy_entry(state):
        return {"default": _clean_entry(state, str(state.get("clock", "")))}
    return {}


def _merge_entries(values: list[dict]) -> dict:
    if not values:
        return _clean_entry({})
    return {
        "streak": max(_state_int(value, "streak") for value in values),
        "step": max(_state_int(value, "step") for value in values),
        "cooldown_until": max(
            _state_float(value, "cooldown_until") for value in values),
        "updated_at": max(
            _state_float(value, "updated_at") for value in values),
    }


def _bounded_entries(entries: dict[str, dict]) -> dict[str, dict]:
    if len(entries) <= _MAX_PROVIDER_ENTRIES:
        return entries
    overflow = entries.pop(_OVERFLOW_PROVIDER, None)
    ordered = sorted(
        entries.items(),
        key=lambda item: (
            _state_float(item[1], "cooldown_until") <= _now(),
            -_state_float(item[1], "updated_at"), item[0]),
    )
    kept = dict(ordered[:_MAX_PROVIDER_ENTRIES - 1])
    omitted = [entry for _, entry in ordered[_MAX_PROVIDER_ENTRIES - 1:]]
    if overflow:
        omitted.append(overflow)
    kept[_OVERFLOW_PROVIDER] = _merge_entries(omitted)
    return kept


def _entries_for_update(state: dict, migration_key: str) -> dict:
    entries = _provider_entries(state)
    if _has_legacy_entry(state) and "default" in entries:
        entries[migration_key] = entries.pop("default")
    state.clear()
    state["providers"] = entries
    return entries


def _entry_for_update(entries: dict[str, dict], identity: str) -> dict:
    if identity in entries:
        return entries[identity]
    if _OVERFLOW_PROVIDER in entries:
        return entries[_OVERFLOW_PROVIDER]
    if len(entries) < _MAX_PROVIDER_ENTRIES:
        entry = _clean_entry({})
        entries[identity] = entry
        return entry
    victim = min(entries, key=lambda key: (
        _state_float(entries[key], "cooldown_until") > _now(),
        _state_float(entries[key], "updated_at"), key))
    entries[_OVERFLOW_PROVIDER] = _merge_entries([entries.pop(victim)])
    return entries[_OVERFLOW_PROVIDER]


def _success_eviction_key(entries: dict[str, dict]) -> str:
    candidates = [key for key in entries if key != _OVERFLOW_PROVIDER]
    return min(candidates, key=lambda key: (
        _state_float(entries[key], "cooldown_until") > _now(),
        _state_float(entries[key], "updated_at"), key))


def _clear_provider_entry(entries: dict[str, dict], identity: str) -> None:
    """Record identity-specific recovery without clearing other overflow.

    Once identities have been conservatively coalesced into the overflow
    partition, removing a recovered identity would make it inherit that
    partition again. Keep a bounded clean exception instead; at capacity an
    older named entry is merged into overflow to make room.
    """
    if _OVERFLOW_PROVIDER not in entries:
        entries.pop(identity, None)
        return
    if identity not in entries and len(entries) >= _MAX_PROVIDER_ENTRIES:
        victim = _success_eviction_key(entries)
        entries[_OVERFLOW_PROVIDER] = _merge_entries([
            entries[_OVERFLOW_PROVIDER], entries.pop(victim)])
    entries[identity] = _clean_entry({})


def _entry_for_read(state: dict, provider: str) -> dict:
    if _has_legacy_entry(state) and not isinstance(state.get("providers"), dict):
        return _clean_entry(state, str(state.get("clock", "")))
    entries = _provider_entries(state)
    return entries.get(provider, entries.get(
        _OVERFLOW_PROVIDER, _clean_entry({})))


def _cooldown_for_step(step: int) -> float:
    base, maximum = _base_cooldown(), _max_cooldown()
    if base <= 0 or base >= maximum:
        return min(base, maximum)
    max_exponent = max(0, math.ceil(math.log2(maximum / base)))
    return min(base * (2 ** min(max(0, step - 1), max_exponent)), maximum)


def register_provider_failure(signature: str = "", data_dir=None,
                              provider: str = "", retry_after: float = 0.0,
                              quota_exhausted: bool = False) -> bool:
    """Record provider health; exhausted quotas trip immediately."""
    limit = threshold()
    if limit <= 0:
        return False
    identity = provider or "default"
    try:
        retry_delay = float(retry_after)
    except (TypeError, ValueError, OverflowError):
        retry_delay = 0.0
    if not math.isfinite(retry_delay) or retry_delay < 0:
        retry_delay = 0.0
    retry_delay = min(retry_delay, _MAX_RETRY_AFTER)

    def update(state):
        entries = _entries_for_update(state, identity)
        entry = _entry_for_update(entries, identity)
        if quota_exhausted:
            step = _state_int(entry, "step") + 1
            cooldown = retry_delay or _quota_cooldown()
            until = max(_state_float(entry, "cooldown_until"), _now() + cooldown)
            entry.update({"streak": 0, "step": step,
                          "cooldown_until": until, "updated_at": _now()})
            return True, cooldown, step
        streak = _streak_after(_state_int(entry, "streak") + 1, limit)
        entry["streak"] = streak
        entry["updated_at"] = _now()
        if streak != 0:
            return False, 0.0, 0
        step = _state_int(entry, "step") + 1
        cooldown = max(_cooldown_for_step(step), retry_delay)
        entry.update({"step": step, "cooldown_until": _now() + cooldown})
        return True, cooldown, step

    with _lock:
        result = _mutate_state(update, data_dir)
    if not result or not result[0]:
        return False
    _, cooldown, step = result
    detail = "quota exhausted" if quota_exhausted else f"{limit} consecutive provider failures"
    log.error(
        "PROVIDER BREAKER [%s]: %s '%s' — cooldown %.0fs (step %d); "
        "campaign admission paused before more provider calls.",
        identity, detail, signature[:80], cooldown, step)
    _emit("provider_breaker", signature[:80], cooldown, step, identity)
    return True


def register_success(data_dir=None, provider: str = "") -> None:
    """Reset one provider; an omitted provider preserves global API semantics."""
    def clear(state):
        if not provider:
            state.clear()
            state["providers"] = {}
            return
        entries = _entries_for_update(state, provider)
        _clear_provider_entry(entries, provider)
    with _lock:
        _mutate_state(clear, data_dir, allow_repair=not provider)


def cooldown_remaining(data_dir=None, provider: str = "") -> float:
    state = _read_state(data_dir)
    if state.get("state_error"):
        return _max_cooldown()
    if provider:
        entries = [_entry_for_read(state, provider)]
    else:
        entries = list(_provider_entries(state).values())
    until = max((_state_float(entry, "cooldown_until")
                 for entry in entries), default=0.0)
    return max(0.0, until - _now())


def _entry_status(entry: dict) -> dict:
    remaining = max(0.0, _state_float(entry, "cooldown_until") - _now())
    return {
        "active": remaining > 0,
        "cooldown_remaining_s": round(remaining, 1),
        "streak": _state_int(entry, "streak"),
        "step": _state_int(entry, "step"),
    }


def breaker_state(data_dir=None, provider: str = "") -> dict:
    """控制台可见状态（ops overview / HTML / TUI）。"""
    state = _read_state(data_dir)
    if provider:
        providers = {provider: _entry_status(_entry_for_read(state, provider))}
    else:
        providers = {key: _entry_status(entry)
                     for key, entry in _provider_entries(state).items()}
    remaining = max((item["cooldown_remaining_s"]
                     for item in providers.values()), default=0.0)
    return {
        "active": remaining > 0,
        "cooldown_remaining_s": remaining,
        "streak": max((item["streak"] for item in providers.values()),
                      default=0),
        "step": max((item["step"] for item in providers.values()), default=0),
        "threshold": threshold(),
        "shared_state_file": _state_path(data_dir),
        "provider_count": len(providers),
        "active_providers": sorted(
            key for key, item in providers.items() if item["active"]),
        "providers": providers,
        "state_error": str(state.get("state_error", "")),
    }


def cooldown_failure(task, data_dir=None) -> Optional[object]:
    """Runner 集成点：冷却中尝试等待（有界）；超限返回失败 TaskResult，
    可执行返回 None。延迟 import 避免循环依赖。"""
    reason = wait_or_fail(300.0, data_dir, provider_identity(task))
    if reason is None:
        return None
    from .models import TaskResult
    return TaskResult(task, False, returncode=-1, reason=reason)


def wait_or_fail(timeout: float, data_dir=None,
                 provider: str = "") -> Optional[str]:
    """任务 spawn 前调用：冷却中则等待（有界）；等待超限返回失败 reason。
    返回 None 表示可以执行。"""
    if threshold() <= 0:
        return None
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        remaining = cooldown_remaining(data_dir, provider)
        if remaining <= 0:
            return None
        budget = max(0.0, deadline - time.monotonic())
        if remaining > budget:
            reason = "provider breaker cooldown exceeded wait budget"
            log.error("PROVIDER BREAKER [%s]: %s",
                      provider or "aggregate", reason)
            _emit("provider_breaker_wait_exceeded", "", remaining, 0,
                  provider or "aggregate")
            return reason
        log.warning(
            "PROVIDER BREAKER [%s]: waiting %.0fs before next spawn "
            "(provider recovering)", provider or "aggregate", remaining)
        time.sleep(min(remaining, budget, 10.0))


def _emit(kind: str, signature: str, cooldown: float, step: int,
          provider: str) -> None:
    try:
        from .metering import record_event
        detail = f"provider={provider} cooldown={cooldown:.0f}s step={step}"
        record_event(kind, signature, detail,
                     config.data_dir())
    except Exception:
        pass  # 事件记录 best-effort
