"""Provider-level circuit breaker（T17a，retro 实战沉淀）。

aero-campaign 复盘（retro --failures-only logs/）暴露真实缺口：agent
失败签名（quota/rate-limit/auth/network）跨任务连续命中 498 次，而
per-task Circuit 与 round 级 StallDetector 都看不见（每任务只失败一
次就换任务）——7×24 无人值守在 provider 系统性故障时会空转烧钱。

本模块：连续 N 次 provider 失败（不同任务也算）→ 进入指数退避冷却；
冷却期间任务 spawn 前等待（有界，超限则以明确 reason 失败而非假装
执行）；任一成功重置窗口。事件进交互账本、日志清晰可见（retro 可
复盘 provider_breaker 行）。

配置（pi-batch.yaml limits.provider_breaker）：
  threshold     连续 provider 失败数（默认 5；0 = 关闭）
  base_cooldown 首次冷却秒数（默认 60，指数退避 ×2）
  max_cooldown  单段冷却上限（默认 900s；超出后任务直接失败）
"""

from __future__ import annotations

import fcntl
import json
import math
import os
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
    raw = handle.read(64 * 1024)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_handle(handle, state: dict) -> None:
    payload = json.dumps(state, separators=(",", ":"),
                         sort_keys=True)
    handle.seek(0)
    handle.truncate()
    handle.write(payload)
    handle.flush()
    os.fsync(handle.fileno())


def _read_state(data_dir=None) -> dict:
    path = Path(_state_path(data_dir))
    try:
        with path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return _read_handle(handle)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        return {}


def _mutate_state(mutator, data_dir=None):
    """原子地锁住共享文件并执行一次 read-modify-write。"""
    path = Path(_state_path(data_dir))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                state = _read_handle(handle)
                result = mutator(state)
                _write_handle(handle, state)
                return result
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return None  # unavailable state must not crash an agent run


def _now() -> float:
    return time.monotonic()


def _settings() -> dict:
    limits = config._section("limits")
    section = limits.get("provider_breaker")
    return section if isinstance(section, dict) else {}


def threshold() -> int:
    value = _settings().get("threshold", _DEFAULT_THRESHOLD)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number >= 0 else 0


def _base_cooldown() -> float:
    value = _settings().get("base_cooldown", _DEFAULT_BASE)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(_DEFAULT_BASE)


def _max_cooldown() -> float:
    value = _settings().get("max_cooldown", _DEFAULT_MAX)
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return float(_DEFAULT_MAX)


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


def register_provider_failure(signature: str = "", data_dir=None) -> bool:
    """记录一次 provider 失败签名拒绝；达到阈值进入共享冷却。"""
    limit = threshold()
    if limit <= 0:
        return False

    def update(state):
        streak = _streak_after(_state_int(state, "streak") + 1, limit)
        state["streak"] = streak
        if streak != 0:
            return False, 0.0, 0
        step = _state_int(state, "step") + 1
        cooldown = min(_base_cooldown() * (2 ** (step - 1)),
                       _max_cooldown())
        state.update({"step": step, "cooldown_until": _now() + cooldown})
        return True, cooldown, step

    with _lock:
        result = _mutate_state(update, data_dir)
    if not result or not result[0]:
        return False
    _, cooldown, step = result
    log.error(
        "PROVIDER BREAKER: %d consecutive provider failures '%s' — "
        "cooldown %.0fs (step %d); tasks will wait before spawning.",
        limit, signature[:80], cooldown, step)
    _emit("provider_breaker", signature[:80], cooldown, step)
    return True


def register_success(data_dir=None) -> None:
    """一次成功重置窗口并立即解除共享冷却。"""
    def clear(state):
        state.clear()
        state.update({"streak": 0, "step": 0, "cooldown_until": 0.0})
    with _lock:
        _mutate_state(clear, data_dir)


def cooldown_remaining(data_dir=None) -> float:
    state = _read_state(data_dir)
    until = _state_float(state, "cooldown_until")
    remaining = until - _now()
    return remaining if remaining > 0 else 0.0


def breaker_state(data_dir=None) -> dict:
    """控制台可见状态（ops overview / HTML / TUI）。"""
    state = _read_state(data_dir)
    until = _state_float(state, "cooldown_until")
    remaining = max(0.0, until - _now())
    return {
        "active": remaining > 0,
        "cooldown_remaining_s": round(remaining, 1),
        "streak": _state_int(state, "streak"),
        "step": _state_int(state, "step"),
        "threshold": threshold(),
        "shared_state_file": _state_path(data_dir),
    }


def cooldown_failure(task, data_dir=None) -> Optional[object]:
    """Runner 集成点：冷却中尝试等待（有界）；超限返回失败 TaskResult，
    可执行返回 None。延迟 import 避免循环依赖。"""
    reason = wait_or_fail(300.0, data_dir)
    if reason is None:
        return None
    from .models import TaskResult
    return TaskResult(task, False, returncode=-1, reason=reason)


def wait_or_fail(timeout: float, data_dir=None) -> Optional[str]:
    """任务 spawn 前调用：冷却中则等待（有界）；等待超限返回失败 reason。
    返回 None 表示可以执行。"""
    if threshold() <= 0:
        return None
    deadline = time.monotonic() + max(0.0, timeout)
    while cooldown_remaining(data_dir) > 0:
        remaining = cooldown_remaining(data_dir)
        if time.monotonic() + remaining > deadline:
            reason = "provider breaker cooldown exceeded wait budget"
            log.error("PROVIDER BREAKER: %s", reason)
            _emit("provider_breaker_wait_exceeded", "", remaining, 0)
            return reason
        log.warning(
            "PROVIDER BREAKER: waiting %.0fs before next spawn "
            "(provider recovering)", remaining)
        time.sleep(min(max(1.0, remaining), 10.0))
    return None


def _emit(kind: str, signature: str, cooldown: float, step: int) -> None:
    try:
        from .metering import record_event
        record_event(kind, signature, f"cooldown={cooldown:.0f}s step={step}",
                     config.data_dir())
    except Exception:
        pass  # 事件记录 best-effort