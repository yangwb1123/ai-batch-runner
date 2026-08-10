#!/usr/bin/env python3
"""UI token 单一事实源加载器（scripts/ 共享，纯标准库，fail closed）。

ui-specs/tokens.json 是 UI token（spacing/radius/color/typography/component）
的规范源。check-ui-geometry.py（圆角家族）与 check-ui-spec.py（间距 token
集）从这里派生各自硬编码的值，消除三份手维护拷贝；漂移由
tests/test_ui_geometry.py 的 pinned 测试（A1/A2）拦截。

失败语义：tokens.json 缺失 / 不可解析 / 家族值非 px 数字 → 抛
TokenLoadError，调用方必须 fail closed（exit 2），错误信息动态携带
实际路径与值。可用环境变量 UI_TOKENS_PATH 显式指定 tokens.json 路径
（测试与移植场景）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

TOKENS_NAME = "ui-specs/tokens.json"
_PX = re.compile(r"^(-?\d+)px")


class TokenLoadError(RuntimeError):
    """tokens.json 缺失或不可解析——调用方必须 fail closed。"""


def _candidates() -> list[Path]:
    """查找顺序：脚本仓库根 ui-specs/ → cwd/ui-specs/。"""
    script_root = Path(__file__).resolve().parent.parent
    return [script_root / TOKENS_NAME, Path.cwd() / TOKENS_NAME]


def load_tokens(path: str = "") -> dict:
    """读取 tokens.json；path 为空时按 UI_TOKENS_PATH → 仓库根 → cwd 查找。"""
    if not path:
        path = os.environ.get("UI_TOKENS_PATH", "")
    candidates = [Path(path)] if path else _candidates()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TokenLoadError(f"ui_tokens: {candidate} 不可解析（{exc}），fail closed")
        if not isinstance(data, dict):
            raise TokenLoadError(f"ui_tokens: {candidate} 顶层必须是 JSON 对象，fail closed")
        return data
    searched = " / ".join(str(p) for p in candidates)
    raise TokenLoadError(
        f"ui_tokens: {TOKENS_NAME} 未找到（已查找: {searched}），"
        f"token 单一事实源缺失，fail closed"
    )


def _int_set(tokens: dict, family: str) -> set[int]:
    """从 token 家族对象派生整数集；缺家族或值非 px 数字 → fail closed。"""
    values = tokens.get(family)
    if not isinstance(values, dict) or not values:
        raise TokenLoadError(f"ui_tokens: tokens.json 缺少 {family} 家族，fail closed")
    result = set()
    for name, raw in values.items():
        match = _PX.match(str(raw))
        if not match:
            raise TokenLoadError(
                f"ui_tokens: tokens.json {family}.{name} 值 '{raw}' 不是 px 数字，fail closed"
            )
        result.add(int(match.group(1)))
    return result


def radius_family(path: str = "") -> set[int]:
    """圆角家族（tokens.json radius.*，如 {4, 8, 12}）。"""
    return _int_set(load_tokens(path), "radius")


def spacing_tokens(path: str = "") -> set[int]:
    """间距 token 集（tokens.json spacing.*，如 {0, 4, 8, 12, ..., 64}）。"""
    return _int_set(load_tokens(path), "spacing")
