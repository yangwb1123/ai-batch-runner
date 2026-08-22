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

import json
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


def _state_path() -> str:
    """跨进程共享状态文件（rate-limit 同款：data_dir 内，env 可覆盖）。
    campaign 多 worktree 子进程共享同一文件 → 熔断跨进程一致。"""
    override = os.environ.get("PBATCH_PROVIDER_BREAKER_FILE", "").strip()
    if override:
        return override
    return str(Path(config.data_dir()) / _STATE_NAME)


def _read_state() -> dict:
    path = _state_path()
    try:
        raw = Path(path).read_text(encoding="utf-8")
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _persist(state: dict) -> None:
    try:
        try:
            handle = open(_state_path(), "r+", encoding="utf-8")
        except FileNotFoundError:
            handle = open(_state_path(), "w", encoding="utf-8")
        with handle:
            payload = json.dumps(state, separators=(",", ":"),
                                 sort_keys=True).encode("utf-8")
            handle.seek(0)
            handle.truncate()
            handle.write(payload.decode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass  # best-effort（与 rate-limit 共享桶同款）


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


def _streak_after(streak: int, limit: int) -> int:
    return 0 if streak >= limit else streak


def register_provider_failure(signature: str = "") -> bool:
    """记录一次 provider 失败签名拒绝；达到阈值进入共享冷却。"""
    limit = threshold()
    if limit <= 0:
        return False
    with _lock:
        state = _read_state()
        streak = _streak_after(int(state.get("streak", 0)) + 1, limit)
        state["streak"] = streak
        if streak == 0:
            step = int(state.get("step", 0) or 0) + 1
            cooldown = min(_base_cooldown() * (2 ** (step - 1)),
                           _max_cooldown())
            state.update({"step": step, "cooldown_until": _now() + cooldown})
            _persist(state)
            log.error(
                "PROVIDER BREAKER: %d consecutive provider failures '%s' — "
                "cooldown %.0fs (step %d); tasks will wait before spawning.",
                limit, signature[:80], cooldown, step)
            _emit("provider_breaker", signature[:80], cooldown, step)
            return True
        _persist(state)
        return False


def register_success() -> None:
    """一次成功重置窗口并立即解除共享冷却。"""
    with _lock:
        _persist({"streak": 0, "step": 0, "cooldown_until": 0.0})


def cooldown_remaining() -> float:
    state = _read_state()
    until = float(state.get("cooldown_until", 0.0) or 0.0)
    if until <= _now():
        return 0.0
    return until - _now()


def breaker_state() -> dict:
    """控制台可见状态（ops overview / HTML 页面）。"""
    remaining = cooldown_remaining()
    state = _read_state()
    return {
        "active": remaining > 0,
        "cooldown_remaining_s": round(remaining, 1),
        "streak": int(state.get("streak", 0)),
        "step": int(state.get("step", 0)),
        "threshold": threshold(),
        "shared_state_file": _state_path(),
    }


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