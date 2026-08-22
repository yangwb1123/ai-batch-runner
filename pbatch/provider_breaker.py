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

import threading
import time
from typing import Optional

from . import config
from .config import log

_DEFAULT_THRESHOLD = 5
_DEFAULT_BASE = 60
_DEFAULT_MAX = 900

_lock = threading.RLock()
_streak: int = 0              # 连续 provider 失败数（跨任务）
_cooldown_until: float = 0.0  # 当前冷却截止（monotonic）
_cooldown_step: int = 0       # 0 = 不在冷却


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


def register_provider_failure(signature: str = "") -> bool:
    """记录一次 provider 失败签名拒绝；返回是否由此进入冷却。"""
    global _streak, _cooldown_until, _cooldown_step
    limit = threshold()
    if limit <= 0:
        return False
    with _lock:
        _streak += 1
        if _streak < limit:
            return False
        _streak = 0
        _cooldown_step += 1
        cooldown = min(_base_cooldown() * (2 ** (_cooldown_step - 1)),
                       _max_cooldown())
        _cooldown_until = time.monotonic() + cooldown
        log.error(
            "PROVIDER BREAKER: %d consecutive provider failures '%s' — "
            "cooldown %.0fs (step %d); tasks will wait before spawning.",
            limit, signature[:80], cooldown, _cooldown_step)
        _emit("provider_breaker", signature[:80], cooldown, _cooldown_step)
        return True


def register_success() -> None:
    """一次成功重置窗口并立即解除冷却（provider 已恢复）。"""
    global _streak, _cooldown_step, _cooldown_until
    with _lock:
        _streak = 0
        _cooldown_step = 0
        _cooldown_until = 0.0


def cooldown_remaining() -> float:
    with _lock:
        if _cooldown_until <= time.monotonic():
            return 0.0
        return _cooldown_until - time.monotonic()


def cooldown_failure(task) -> Optional[object]:
    """Runner 集成点：冷却中尝试等待（有界）；超限返回失败 TaskResult，
    可执行返回 None。延迟 import 避免循环依赖。"""
    reason = wait_or_fail(300.0)
    if reason is None:
        return None
    from .models import TaskResult
    return TaskResult(task, False, returncode=-1, reason=reason)


def wait_or_fail(timeout: float) -> Optional[str]:
    """任务 spawn 前调用：冷却中则等待（有界）；等待超限返回失败 reason。
    返回 None 表示可以执行。"""
    if threshold() <= 0:
        return None
    deadline = time.monotonic() + max(0.0, timeout)
    while cooldown_remaining() > 0:
        remaining = cooldown_remaining()
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