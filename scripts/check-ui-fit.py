#!/usr/bin/env python3
"""UI fit & accessible-name gate (pure stdlib).

Two defect classes that the spacing/color/geometry rules structurally cannot
see, because the evidence lives in *two* places that no single-file scan can
join:

``control-overflow``
    A table column narrower than the control rendered inside its cell. The
    control's minimum width is not in the project source at all -- it is an
    inline ``minWidth`` inside the installed component package -- so this
    rule parses that package instead of hard-coding a table that silently
    rots on the next Iris upgrade.

``control-accessible-name``
    A form control with neither ``aria-label``/``aria-labelledby`` nor an
    enclosing ``<label>``. In a table cell the column header is *not* an
    accessible name: it is visual proximity, not a programmatic association,
    so an inline editor per row is unreachable by name for assistive tech.

Known boundary: the width rule only pairs a cell renderer with a column declared
in the *same file*. When a dialog keeps its columns in a sibling module the pair
is invisible to this rule and the renderer is skipped rather than guessed. That
is a coverage gap, not a pass -- the accessible-name rule still applies there.

Both rules are deliberately conservative. A false positive here costs more
than a missed defect: a gate that cries wolf gets ignored, which is worse
than not having it. So the control-width registry is read from the package
and falls back to "unknown -> skip", and the accessible-name rule accepts a
``<label>`` ancestor anywhere in the file.

Usage:
    python scripts/check-ui-fit.py --dir apps/web/src --json
    python scripts/check-ui-fit.py apps/web/src/pages/aps/settings/Widget.tsx

Exit 0 = clean, 1 = violations, 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

SOURCE_EXTENSIONS = {".tsx", ".ts", ".jsx", ".vue"}
_TEST_FILE_RE = re.compile(r"\.(test|spec)\.")

FORM_CONTROLS = (
    "IrisInput",
    "IrisNumberInput",
    "IrisSelect",
    "IrisDatePicker",
    "IrisTimePicker",
    "IrisSwitch",
    "IrisCheckbox",
    "IrisMonthPicker",
    "IrisSearchInput",
)

REGISTRY = Path(__file__).resolve().parent.parent / "ui-specs" / "control-fit.json"

IRIS_PACKAGE_GLOBS = (
    "node_modules/@iris-ui-kit/vue/dist/index.js",
    "node_modules/.pnpm/@iris-ui-kit+vue@*/node_modules/@iris-ui-kit/vue/dist/index.js",
)

# 一行式组件定义的边界：dist 里的顶层定义都在行首。
_TOP_LEVEL_DEF = re.compile(r"^var (\w+) = (?:/\*@__PURE__\*/\s*)?defineComponent\(", re.M)


def component_segment(source: str, name: str) -> Optional[str]:
    """该组件自己的定义体：起点是它的顶层定义，终点是下一个顶层定义。"""
    bounds = [(m.group(1), m.start()) for m in _TOP_LEVEL_DEF.finditer(source)]
    for index, (defined, start) in enumerate(bounds):
        if defined != name:
            continue
        end = bounds[index + 1][1] if index + 1 < len(bounds) else len(source)
        return source[start:end]
    return None


def _registry_entries() -> dict:
    try:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = registry.get("controls")
    return entries if isinstance(entries, dict) else {}


def _verified(source: str, entries: dict) -> dict[str, int]:
    """Keep only the entries whose evidence still matches the installed build."""
    widths: dict[str, int] = {}
    for control, spec in entries.items():
        if not isinstance(spec, dict):
            continue
        evidence = spec.get("evidence")
        value = spec.get("min_width")
        if not isinstance(evidence, str) or not isinstance(value, int):
            continue
        segment = component_segment(source, control)
        if segment is None or re.search(evidence, segment) is None:
            continue
        widths[control] = value
    return widths


def _installed_bundle(repo_root: Path) -> str:
    for pattern in IRIS_PACKAGE_GLOBS:
        for candidate in sorted(repo_root.glob(pattern)):
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return ""


def control_min_widths(repo_root: Path) -> dict[str, int]:
    """Registry entries verified against the installed package, or dropped.

    Scraping a minified bundle for "the component's min-width" is unsound: a
    fixed character window bleeds neighbouring components in (measured:
    IrisSwitch read back as 140px when its own track is 28/36/44). So the
    numbers live in ``ui-specs/control-fit.json`` with an evidence pattern,
    and each entry is re-checked inside that component's own definition body
    before it is believed. A mismatch drops the entry -- the rule then skips
    instead of reporting a number nobody can defend.
    """
    entries = _registry_entries()
    if not entries:
        return {}
    source = _installed_bundle(repo_root)
    return _verified(source, entries) if source else {}


def label_spans(source: str) -> list[tuple[int, int]]:
    """Byte ranges covered by a <label>…</label> pair (best effort, non-nested safe)."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start: Optional[int] = None
    for match in re.finditer(r"<label\b|</label>", source):
        if match.group(0) == "<label":
            if depth == 0:
                start = match.start()
            depth += 1
        else:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append((start, match.end()))
                start = None
    return spans


def tag_end(source: str, open_index: int) -> int:
    """Index just past the '>' closing the tag that starts at ``open_index``."""
    cursor = open_index
    quote: Optional[str] = None
    while cursor < len(source):
        char = source[cursor]
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == ">":
            return cursor + 1
        cursor += 1
    return len(source)


def declared_columns(source: str) -> dict[str, int]:
    """column key -> declared pixel width, from an IrisTableColumn literal."""
    columns: dict[str, int] = {}
    for match in re.finditer(
        r'key:\s*"([A-Za-z_][\w]*)"(?P<body>[^}\]]{0,320}?)width:\s*(\d+)',
        source,
    ):
        columns[match.group(1)] = int(match.group(3))
    return columns


def cell_slot_bodies(source: str) -> dict[str, tuple[int, str]]:
    """column key -> (line offset, body) for every `cell.<key>` renderer."""
    bodies: dict[str, tuple[int, str]] = {}
    marks = [(m.group(1), m.start()) for m in re.finditer(r'"cell\.([A-Za-z_][\w]*)"\s*:', source)]
    for index, (key, start) in enumerate(marks):
        end = marks[index + 1][1] if index + 1 < len(marks) else len(source)
        bodies[key] = (source.count("\n", 0, start) + 1, source[start:end])
    return bodies


def dynamic_slot_keys(source: str) -> list[tuple[str, list[str], str]]:
    """Key arrays whose entries become `cell.<key>` slots, e.g. lineSwitchKeys."""
    found: list[tuple[str, list[str], str]] = []
    pattern = re.compile(
        r"(?:const|let)\s+(\w+)[^=]*=\s*(?:\[[^\]]*\]|as const\s*\[)(?P<body>[^\]]*)\]",
        re.S,
    )
    for match in pattern.finditer(source):
        name = match.group(1)
        keys = re.findall(r'"([A-Za-z_][\w]*)"', match.group("body"))
        if not keys:
            continue
        # Only trust arrays that are actually turned into cell.<key> slots.
        window = source[match.end(): match.end() + 1200]
        if f"cell.${{{name}}}" in window or f"cell.{name}" in window:
            found.append((name, keys, match.group(0)))
    return found


def controls_in(body: str) -> set[str]:
    return {name for name in FORM_CONTROLS if f"<{name}" in body}


def native_inputs_in(body: str) -> int:
    return len(re.findall(r"<input\b", body))


def accessible_name_missing(source: str) -> list[tuple[int, str]]:
    """Controls with neither an aria name nor an enclosing <label>."""
    spans = label_spans(source)
    missing: list[tuple[int, str]] = []
    for match in re.finditer(r"<(" + "|".join(FORM_CONTROLS) + r")\b", source):
        end = tag_end(source, match.end() - 1)
        fragment = source[match.end():end]
        if re.search(r"aria-label|ariaLabel|aria-labelledby|ariaDescribedby", fragment):
            continue
        if any(start <= match.start() < stop for start, stop in spans):
            continue
        missing.append((source.count("\n", 0, match.start()) + 1, match.group(1)))
    return missing


def _overflow_violations(
    source: str, widths: dict[str, int], relative: str
) -> list[dict]:
    violations: list[dict] = []
    columns = declared_columns(source)

    for key, (line, body) in cell_slot_bodies(source).items():
        width = columns.get(key)
        if width is None:
            continue  # column declared elsewhere: another file owns that pair
        for control in sorted(controls_in(body)):
            minimum = widths.get(control)
            if minimum is not None and minimum > width:
                violations.append({
                    "file": relative,
                    "line": line,
                    "rule": "control-overflow",
                    "detail": (
                        f"列 {key} 宽 {width}px，装不下 {control}"
                        f"（该控件 inline min-width {minimum}px）"
                    ),
                })
    return violations + _composed_overflow(source, columns, widths, relative)


def _composed_overflow(
    source: str, columns: dict, widths: dict[str, int], relative: str
) -> list[dict]:
    """Cell slots built from a key array (e.g. one switch per connection flag)."""
    violations: list[dict] = []
    for array_name, keys, _raw in dynamic_slot_keys(source):
        composer = re.search(
            r"cell\.\$\{" + re.escape(array_name) + r"\}(?P<body>[\s\S]{0,1200})",
            source,
        )
        if not composer:
            continue
        used = controls_in(composer.group("body"))
        line = source.count("\n", 0, composer.start()) + 1
        for key in keys:
            width = columns.get(key)
            if width is None:
                continue
            for control in sorted(used):
                minimum = widths.get(control)
                if minimum is not None and minimum > width:
                    violations.append({
                        "file": relative,
                        "line": line,
                        "rule": "control-overflow",
                        "detail": (
                            f"列 {key} 宽 {width}px，装不下 {control}"
                            f"（{array_name} 批量插槽，该控件 min-width {minimum}px）"
                        ),
                    })
    return violations


def check_file(path: Path, widths: dict[str, int], relative: str) -> list[dict]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [{"file": relative, "line": 0, "rule": "read-error", "detail": str(exc)}]

    names = [
        {"file": relative, "line": line, "rule": "control-accessible-name",
         "detail": f"{control} 既无 aria-label 也无 <label> 包裹"}
        for line, control in accessible_name_missing(source)
    ]
    return _overflow_violations(source, widths, relative) + names


def _relative(target: Path, repo_root: Path) -> str:
    try:
        return str(target.resolve().relative_to(repo_root))
    except ValueError:
        return str(target)


def iter_sources(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.suffix in SOURCE_EXTENSIONS and path.is_file():
            # 注意 `Widget.test.tsx` 的 stem 是 "Widget.test"（没有尾点），
            # 只查 stem 里的 ".test." 会漏掉整类测试文件。
            if "__testkit__" in path.parts:
                continue
            if _TEST_FILE_RE.search(path.name) or path.stem.endswith((".test", ".spec")):
                continue
            yield path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", nargs="?", help="single file to check")
    parser.add_argument("--dir", dest="directory", help="scan directory recursively")
    parser.add_argument("--repo-root", default=".", help="where to look for node_modules")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--offline", action="store_true",
                        help="skip package verification (width rules go quiet)")
    args = parser.parse_args(argv)

    if not args.artifact and not args.directory:
        print("usage: check-ui-fit.py <file> | --dir DIR [--json]", file=sys.stderr)
        return 2

    repo_root = Path(args.repo_root).resolve()
    widths = control_min_widths(repo_root)
    if args.offline and not widths:
        widths = {}

    if args.artifact:
        targets = [Path(args.artifact)]
    else:
        targets = list(iter_sources(Path(args.directory)))

    violations: list[dict] = []
    for target in targets:
        if not target.is_file():
            print(f"not a file: {target}", file=sys.stderr)
            return 2
        violations.extend(check_file(target, widths, _relative(target, repo_root)))

    if args.json:
        print(json.dumps({
            "files": len(targets),
            "violations": violations,
            "total": len(violations),
            "control_min_widths": widths,
            "width_source": "verified-registry" if widths else "unverified",
        }, ensure_ascii=False, indent=2))
    else:
        for item in violations:
            print(f"UI-FIT {item['file']}:{item['line']}: {item['detail']}")
        if violations:
            print(f"UI-FIT: {len(violations)} violation(s); rejected")
        else:
            print(f"UI-FIT: OK ({len(targets)} file(s) scanned)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
