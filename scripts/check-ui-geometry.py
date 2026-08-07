#!/usr/bin/env python3
"""UI 几何规范机械检查（纯标准库，fail closed）。

对应 ui-specs/geometry.md。拦截三类可机械判定的几何问题：

1. 裸数字小偏移：1-3px 的 margin-top/left/right/bottom 或 translateX/Y，
   未绑定 --optical-* 变量（光学修正必须显式声明，禁止散落偏移）；
2. 随机圆角：border-radius 值不在 {6, 10, 16} 家族（且非 50%/9999px 圆）；
3. 随机线条宽度：border 宽度不在 {1, 2, 3} 层级（0/None 除外）。

业务 UI 契约（business-ui-contract.md）属评审角色裁决，不在此机械检查。

用法：
    python scripts/check-ui-geometry.py <artifact.tsx|.css|.dart|.vue>
    python scripts/check-ui-geometry.py --dir <目录> [--all] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

OPTICAL_OK = re.compile(r"--optical-[a-z0-9-]+")
OFFSET_PATTERN = re.compile(
    r"""(?:margin(?:-top|-left|-right|-bottom|Top|Left|Right|Bottom)?|translate[XY]?|"""
    r"""inset(?:-inline|-block|Inline|Block)?(?:-start|-end|Start|End)?)\s*:\s*"""
    r"""["']?(?P<value>-?\d{1,2})px""",
)
RADIUS_PATTERN = re.compile(r"""border(?:-radius|Radius)\s*:\s*["']?([^;"',}]+)["']?["'`,;}]*""")
BORDER_WIDTH_PATTERN = re.compile(
    r"""border(?:-top|-left|-right|-bottom|Top|Left|Right|Bottom)?(?:-width|Width)\s*:\s*"""
    r"""["']?(\d+(?:\.\d+)?)px""",
)
RADIUS_FAMILY = {6, 10, 16}
STROKE_LEVELS = {1, 2, 3}
ALLOWED_OFFSETS = {0}  # 0 是合法的重置

def check_offsets(text: str) -> list[str]:
    """裸数字小偏移（1-3px）未绑定光学变量 → 问题。"""
    problems = []
    # 逐行检查：同一行若出现 --optical-* 变量则豁免
    lines = text.splitlines()
    # sr-only 无障碍隐藏模式（clip: rect(...) 移出视口）不是光学偏移——
    # 块级判断：clip 声明所在行及其前后 5 行内的 margin 偏移豁免。
    clip_blocks: set[int] = set()
    for i, line in enumerate(lines):
        if "clip:" in line:
            for j in range(max(0, i - 5), min(len(lines), i + 6)):
                clip_blocks.add(j + 1)
    for lineno, line in enumerate(lines, 1):
        if "--optical" in line or "--spacing" in line or "var(--" in line:
            continue
        if lineno in clip_blocks:
            continue
        for match in OFFSET_PATTERN.finditer(line):
            value = int(match.group("value"))
            if value in ALLOWED_OFFSETS:
                continue
            if 1 <= abs(value) <= 3:
                problems.append(
                    f"{lineno}:{match.start() + 1}: bare offset "
                    f"{match.group(0).split(':', 1)[0]}: {match.group('value')}px"
                    f" — 1-3px 光学修正必须绑定 --optical-* 变量"
                    f"（ui-specs/geometry.md §5）"
                )
    return problems


def check_radius(text: str) -> list[str]:
    """border-radius 必须属于 {6,10,16} 家族（或 50%/9999px/0）。"""
    problems = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = RADIUS_PATTERN.search(line)
        if not match:
            continue
        raw = match.group(1).strip().removesuffix("!important").strip()
        if raw in {"50%", "9999px", "999px", "0", "0px", "inherit"} or raw.endswith("%"):
            continue
        if raw.startswith("var("):
            continue
        values = [v.strip() for v in raw.split()]
        for value in values:
            if value.endswith("px"):
                try:
                    number = int(value[:-2])
                except ValueError:
                    continue
                if number not in RADIUS_FAMILY:
                    problems.append(
                        f"{lineno}:{match.start() + 1}: border-radius {number}px "
                        f"不在圆角家族 {{6,10,16}}（ui-specs/geometry.md §4）"
                    )
    return problems


def check_strokes(text: str) -> list[str]:
    """border 宽度必须属于 {1,2,3} 层级。"""
    problems = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in BORDER_WIDTH_PATTERN.finditer(line):
            try:
                width = float(match.group(1))
            except ValueError:
                continue
            if width not in STROKE_LEVELS:
                problems.append(
                    f"{lineno}:{match.start() + 1}: border width {width}px "
                    f"不在线条层级 {{1,2,3}}（ui-specs/geometry.md §3）"
                )
    return problems


def check_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    problems = []
    problems.extend(check_offsets(text))
    problems.extend(check_radius(text))
    problems.extend(check_strokes(text))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact", nargs="?")
    ap.add_argument("--dir", default="")
    ap.add_argument("--all", action="store_true", help="scan all UI source files under --dir")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    targets: list[Path] = []
    if args.dir:
        root = Path(args.dir)
        suffixes = {".tsx", ".jsx", ".vue", ".css", ".dart"}
        if args.all:
            targets = [p for p in root.rglob("*") if p.suffix in suffixes and "node_modules" not in p.parts]
        else:
            targets = [p for p in root.iterdir() if p.suffix in suffixes]
    elif args.artifact:
        targets = [Path(args.artifact)]
    else:
        print("用法: check-ui-geometry.py <artifact> | --dir <dir> [--all]", file=sys.stderr)
        return 2

    all_problems: dict[str, list[str]] = {}
    for path in targets:
        if not path.is_file():
            continue
        problems = check_file(path)
        if problems:
            all_problems[str(path)] = problems

    if args.json:
        print(json.dumps({"files": len(targets), "violations": all_problems}, ensure_ascii=False, indent=1))
    else:
        for path, problems in all_problems.items():
            print(f"uigeometry: {path}")
            for problem in problems:
                print(f"  ✗ {problem}")
        if all_problems:
            total = sum(len(v) for v in all_problems.values())
            print(f"uigeometry: FAIL — {total} violation(s) across {len(all_problems)} file(s)", file=sys.stderr)
            return 1
        print(f"uigeometry: PASS ({len(targets)} file(s) clean)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
