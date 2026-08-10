#!/usr/bin/env python3
"""UI 几何规范机械检查（纯标准库，fail closed）。

对应 ui-specs/geometry.md。拦截三类可机械判定的几何问题：

1. 裸数字小偏移：1-3px 的 margin-top/left/right/bottom 或 translateX/Y，
   未绑定 --optical-* 变量（光学修正必须显式声明，禁止散落偏移）；
2. 随机圆角：border-radius 值不在 tokens.json 圆角家族（{4,8,12}）
   （且非 50%/9999px 圆）；
3. 随机线条宽度：border 宽度不在 {1, 2, 3} 层级（0/None 除外）。

业务 UI 契约（business-ui-contract.md）属评审角色裁决，不在此机械检查。

用法：
    python scripts/check-ui-geometry.py <artifact.tsx|.css|.dart|.vue>
    python scripts/check-ui-geometry.py --dir <目录> [--all] [--json] [-o 报告.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

import ui_tokens

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
_radius_family: Optional[set[int]] = None
STROKE_LEVELS = {1, 2, 3}
ALLOWED_OFFSETS = {0}  # 0 是合法的重置



def get_radius_family() -> set[int]:
    """圆角家族：tokens.json radius.* 派生，惰性解析（import 无副作用）。"""
    global _radius_family
    if _radius_family is None:
        _radius_family = ui_tokens.radius_family()
    return _radius_family


def family_label(values: set[int]) -> str:
    """{4,8,12} 形式的错误信息标签。"""
    return "{" + ",".join(str(v) for v in sorted(values)) + "}"

def _offset_line_exempt(line: str) -> bool:
    """同一行出现 --optical-* 变量或 sr-only 裁剪时豁免。"""
    return "--optical-" in line or "clip: rect" in line


def _clip_blocks(lines: list) -> set:
    """clip: rect(...) 移出视口的行号（含前后 5 行）——sr-only 无障碍隐藏
    不是光学偏移，块级豁免。"""
    blocks = set()
    for i, line in enumerate(lines):
        if "clip:" not in line:
            continue
        for j in range(max(0, i - 5), min(len(lines), i + 6)):
            blocks.add(j + 1)
    return blocks


def _offset_problem(match) -> Optional[str]:
    """单处裸偏移判定：合法重置或非 1-3px 返回 None。"""
    value = int(match.group("value"))
    if value in ALLOWED_OFFSETS:
        return None
    if not (1 <= abs(value) <= 3):
        return None
    return (f"{match.start() + 1}: bare offset "
            f"{match.group(0).split(':', 1)[0]}: {match.group('value')}px"
            f" — 1-3px 光学修正必须绑定 --optical-* 变量"
            f"（ui-specs/geometry.md §5）")


def check_offsets(text: str) -> list[str]:
    """裸数字小偏移（1-3px）未绑定光学变量 → 问题。"""
    problems = []
    lines = text.splitlines()
    clip_blocks = _clip_blocks(lines)
    for lineno, line in enumerate(lines, 1):
        if "--spacing" in line or "var(--" in line or _offset_line_exempt(line):
            continue
        if lineno in clip_blocks:
            continue
        for match in OFFSET_PATTERN.finditer(line):

            problem = _offset_problem(match)
            if problem:
                problems.append(f"{lineno}:{problem}")
    return problems


def _radius_skipped(raw: str) -> bool:
    """圆角合法值：50%/9999px/0/inherit/var()/百分比——跳过。"""
    return (raw in {"50%", "9999px", "999px", "0", "0px", "inherit"}
            or raw.endswith("%") or raw.startswith("var("))


def check_radius(text: str) -> list[str]:
    """border-radius 必须属于 tokens.json 圆角家族（或 50%/9999px/0）。"""
    family = get_radius_family()
    label = family_label(family)
    problems = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = RADIUS_PATTERN.search(line)
        if not match:
            continue
        raw = match.group(1).strip().removesuffix("!important").strip()

        if _radius_skipped(raw):
            continue
        for value in (v.strip() for v in raw.split()):
            if not value.endswith("px"):
                continue
            try:
                number = int(value[:-2])
            except ValueError:
                continue
            if number not in family:
                problems.append(
                    f"{lineno}:{match.start() + 1}: border-radius {number}px "
                    f"不在圆角家族 {label}（tokens.json radius / ui-specs/geometry.md §4）"
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


def _collect_targets(args) -> tuple[list[Path], bool]:
    """目标文件解析；用法错误时返回 (空, False)。"""
    if args.dir:
        root = Path(args.dir)
        suffixes = {".tsx", ".jsx", ".vue", ".css", ".dart"}
        if args.all:
            return [p for p in root.rglob("*")
                    if p.suffix in suffixes and "node_modules" not in p.parts], True
        return [p for p in root.iterdir() if p.suffix in suffixes], True
    if args.artifact:
        return [Path(args.artifact)], True
    print("用法: check-ui-geometry.py <artifact> | --dir <dir> [--all]", file=sys.stderr)
    return [], False


def _scan(targets: list[Path]) -> dict[str, list[str]]:
    """逐文件检查；只保留有问题的文件。"""
    all_problems: dict[str, list[str]] = {}
    for path in targets:
        if not path.is_file():
            continue
        problems = check_file(path)
        if problems:
            all_problems[str(path)] = problems
    return all_problems


def _emit(args, targets: list[Path], all_problems: dict[str, list[str]]) -> int:
    """--json/-o 机器报告或人类摘要；返回退出码（0 通过 / 1 违规）。

    与 check-ui-spec.py 对齐：--json 模式下违规同样 exit 1（fail closed），
    摘要写 stderr 供 runner 重试循环回填。"""
    payload = json.dumps({"files": len(targets), "violations": all_problems},
                         ensure_ascii=False, indent=1)
    if args.o:
        out_path = Path(args.o)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
        if all_problems:
            total = sum(len(v) for v in all_problems.values())
            print(f"uigeometry: FAIL — {total} violation(s) across "
                  f"{len(all_problems)} file(s)", file=sys.stderr)
        return 0 if not all_problems else 1
    for path, problems in all_problems.items():
        print(f"uigeometry: {path}")
        for problem in problems:
            print(f"  ✗ {problem}")
    if all_problems:
        total = sum(len(v) for v in all_problems.values())
        print(f"uigeometry: FAIL — {total} violation(s) across "
              f"{len(all_problems)} file(s)", file=sys.stderr)
        return 1
    print(f"uigeometry: PASS ({len(targets)} file(s) clean)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact", nargs="?")
    ap.add_argument("--dir", default="")
    ap.add_argument("--all", action="store_true", help="scan all UI source files under --dir")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-o", default="", help="JSON 报告写入路径（与 --json 同格式）")
    args = ap.parse_args()

    # 圆角家族 fail closed：tokens.json 缺失/不可解析 → exit 2（动态错误信息）
    try:
        get_radius_family()
    except ui_tokens.TokenLoadError as exc:
        print(f"uigeometry: {exc}", file=sys.stderr)
        return 2

    targets, ok = _collect_targets(args)
    if not ok:
        return 2
    return _emit(args, targets, _scan(targets))


if __name__ == "__main__":
    sys.exit(main())
