"""Evidence-only execution report projection; never execution or acceptance policy.

The caller owns interval measurement. Context-local timing keeps historical
one-argument summary hooks usable without modifying results or global state.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from .models import TaskResult
from .memory_records import redact


_SUMMARY_WALL: ContextVar[tuple] = ContextVar("summary_wall", default=(None, "execution"))


def _seconds(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _duration(value: object) -> str:
    seconds = _seconds(value)
    return "unknown (not measured)" if seconds is None else f"{seconds:.1f}s"


def _wall_label(scope: object) -> str:
    return f"{scope} execution wall" if scope in ("batch", "pipeline") else "execution wall"


@contextmanager
def summary_wall(seconds: object, scope: str):
    """Bind caller-measured evidence only for the synchronous summary hook."""
    token = _SUMMARY_WALL.set((_seconds(seconds), scope))
    try:
        yield
    finally:
        _SUMMARY_WALL.reset(token)


def _cumulative_elapsed(results: list[TaskResult]) -> Optional[float]:
    elapsed = [_seconds(result.elapsed) for result in results]
    if any(value is None for value in elapsed):
        return None
    return _seconds(sum(elapsed))


def print_summary(results: list[TaskResult], wall_seconds: Optional[float] = None,
                  *, wall_scope: str = "execution") -> None:
    """Project returned task elapsed, optional measured execution wall, unknown CPU.

    ``wall_seconds`` must be finite and nonnegative; invalid values stay unknown.
    Without explicit timing, a CLI-scoped measurement is used when available.
    Neither the sum nor the maximum of task durations measures execution wall.
    """
    if wall_seconds is None:
        wall_seconds, wall_scope = _SUMMARY_WALL.get()
    total = len(results)
    succeeded = sum(1 for result in results if result.success)
    failed = total - succeeded
    print("=" * 56)
    print("  abr execution report")
    print("=" * 56)
    print("  total:     %d" % total)
    print("  succeeded: %d" % succeeded)
    print("  failed:    %d" % failed)
    print("  cumulative task elapsed wall: %s" % _duration(_cumulative_elapsed(results)))
    print("  %s: %s" % (_wall_label(wall_scope), _duration(wall_seconds)))
    print("  CPU time: unknown (not measured)")
    if total:
        rate = 100.0 * succeeded / total
        print("  成功率:    %.0f%%" % rate)
    if failed:
        print("  下一步:    重试用 --retries；被拒产物在用户级 runtime/rejected/ 可诊断")
    elif succeeded:
        print("  任务结果通过；仓库门禁、合并与目标验收仍需分别裁决")
    print()
    for result in results:
        icon = "PASS" if result.success else "FAIL"
        brief = redact(result.task.prompt)[:60].replace("\n", " ")
        sig = (f"  [val exit={result.validation_exit}]"
               if not result.success and result.validation_exit else "")
        print("  %s  [%7s]%s %s..." % (icon, _duration(result.elapsed), sig, brief))
    print("=" * 56)
