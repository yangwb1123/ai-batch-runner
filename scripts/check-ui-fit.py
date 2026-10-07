#!/usr/bin/env python3
"""UI fit & accessible-name gate for TSX/TS sources (pure stdlib).

The stylesheet half (CSS cancelling a control's own minimum width) lives in
``ui_fit_css.py``; this module handles the component-source half.

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
in the same file; a dialog keeping its columns in a sibling module is skipped.

Both rules are deliberately conservative. A false positive costs more than a
missed defect -- a gate that cries wolf gets ignored. So an unmeasured control
is skipped rather than guessed, and a ``<label>`` ancestor counts as a name.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_fit_css import min_width_overrides  # noqa: E402
from ui_fit_registry import (  # noqa: E402
    control_min_widths,
    names_control_map,
    installed_vue_package,
)
from ui_fit_names import installed_name_routing  # noqa: E402
from ui_fit_jsx import (  # noqa: E402
    accessible_name_missing as _native_name_missing,
    blank_comments, tag_end, tags_in, attribute,
)

SOURCE_EXTENSIONS = {".tsx", ".ts", ".jsx", ".vue", ".css"}
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

def label_spans(source: str) -> list[tuple[int, int]]:
    """Byte ranges covered by a <label>…</label> pair (best effort, non-nested safe)."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start: Optional[int] = None
    for match in re.finditer(r"<label\b|</label>", blank_comments(source)):
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


def declared_columns(source: str) -> dict[str, int]:
    """column key -> declared pixel width, from an IrisTableColumn literal."""
    columns: dict[str, int] = {}
    for match in re.finditer(
        r'''key:\s*["']([A-Za-z_][\w]*)["'](?P<body>[^}\]]{0,320}?)width:\s*(\d+)''',
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


def accessible_name_missing(
    source: str, swallows: dict[str, bool], form_fields: Optional[set[str]] = None,
    camel: Optional[dict[str, bool]] = None,
) -> list[tuple[int, str]]:
    return _native_name_missing(source, FORM_CONTROLS, swallows, form_fields, camel)


def _slot_minimum(control: str, body: str, widths: dict, probe: dict) -> Optional[int]:
    tiers = probe.get("widths_by_size", {}).get(control, {})
    if not tiers:
        return widths.get(control)
    minima = []
    for tag in tags_in(body):
        if tag.name == control:
            size = attribute(tag.attrs, "size")
            explicit = size.removeprefix("literal:") if size and size.startswith("literal:") else None
            minima.append(tiers[explicit] if explicit in tiers else max(tiers.values()))
    return max(minima) + probe.get("cell_padding", 0) if minima else None


def _column_evidence(path: Path, source: str) -> dict:
    columns = declared_columns(source)
    for match in re.finditer(r'''from\s+["'](\./[^"']+\.columns)["']''', blank_comments(source)):
        sibling = path.parent / (match.group(1) + ".ts")
        try:
            if sibling.resolve().parent == path.parent.resolve() and sibling.stat().st_size <= 512 * 1024:
                columns = {**declared_columns(sibling.read_text(encoding="utf-8")), **columns}
        except (OSError, UnicodeError):
            continue
    return columns


def _overflow_violations(
    source: str, widths: dict[str, int], relative: str,
    columns: Optional[dict] = None, probe: Optional[dict] = None,
) -> list[dict]:
    violations: list[dict] = []
    columns = declared_columns(source) if columns is None else columns

    for key, (line, body) in cell_slot_bodies(source).items():
        width = columns.get(key)
        if width is None:
            continue  # column declared elsewhere: another file owns that pair
        for control in sorted(controls_in(body)):
            minimum = _slot_minimum(control, body, widths, probe or {})
            if minimum is not None and minimum > width:
                violations.append({
                    "file": relative,
                    "line": line,
                    "rule": "control-overflow",
                    "detail": (
                        f"列 {key} 宽 {width}px，装不下 {control}"
                        f"（完整控件最小占位含已知单元格 padding {minimum}px）"
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


def check_file(
    path: Path, widths: dict[str, int], relative: str,
    swallows: Optional[dict[str, bool]] = None,
    form_fields: Optional[set[str]] = None,
    camel: Optional[dict[str, bool]] = None,
    probe: Optional[dict] = None,
) -> list[dict]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [{"file": relative, "line": 0, "rule": "read-error", "detail": str(exc)}]

    names = [
        {"file": relative, "line": line, "rule": "control-accessible-name",
         "detail": detail}
        for line, detail in accessible_name_missing(source, swallows or {}, form_fields, camel)
    ]
    css = (min_width_overrides(source, widths, relative)
           if path.suffix == ".css" else [])
    return _overflow_violations(source, widths, relative, _column_evidence(path, source), probe) + css + names


def _relative(target: Path, repo_root: Path) -> str:
    try:
        return str(target.resolve().relative_to(repo_root))
    except ValueError:
        return str(target)


def css_rules(source: str) -> list[tuple[str, str]]:
    """(selector, declaration block) for every flat CSS rule."""
    cleaned = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return [
        (match.group(1).strip(), match.group(2))
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", cleaned)
    ]


# data-iris-* 属性 → 组件名。Iris 把这些属性打在**真正承载交互**的元素上，
# 所以"选择器命中它"就等于"这条规则作用在该控件上"。
_TRIGGER_ATTRS = {
    "data-iris-select-trigger": "IrisSelect",
    "data-iris-month-picker-trigger": "IrisMonthPicker",
    "data-iris-number-input": "IrisNumberInput",
    "data-iris-input": "IrisInput",
    "data-iris-switch": "IrisSwitch",
    "data-iris-checkbox": "IrisCheckbox",
    "data-iris-date-picker-trigger": "IrisDatePicker",
}


def _css_control(selector: str) -> Optional[str]:
    for attr, control in _TRIGGER_ATTRS.items():
        if attr in selector:
            return control
    return None


def _css_min_width_overrides(
    source: str, widths: dict[str, int], relative: str
) -> list[dict]:
    """CSS 把控件的最小宽度压到 0 —— 组件自己的下限就此失效。

    这是"label 文字超出组件宽度""控件挤成一团"的直接成因：IrisSelect 声明
    min-width:140px，页面 CSS 再写 `min-width: 0 !important`，窄容器里触发器
    就被压到比内容还窄。
    """
    violations: list[dict] = []
    for selector, block in css_rules(source):
        control = _css_control(selector)
        if control is None:
            continue
        minimum = widths.get(control)
        if minimum is None:
            continue
        if not re.search(r"min-width:\s*0\s*(?:!important)?\s*;", block):
            continue
        violations.append({
            "file": relative,
            "line": source.count("\n", 0, source.find(selector)) + 1,
            "rule": "control-min-width-override",
            "detail": (
                f"{selector} 把 {control} 的 min-width 压到 0，"
                f"但该控件声明的下限是 {minimum}px"
            ),
        })
    return violations


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


def _name_evidence(repo_root: Path, offline: bool) -> tuple[dict, dict, set, dict]:
    probe = installed_name_routing(None if offline else installed_vue_package(repo_root), FORM_CONTROLS)
    routing = probe["controls"]
    swallows = names_control_map()
    swallows.update({name: data["aria"] for name, data in routing.items()})
    camel = {"IrisCheckbox": True}
    camel.update({name: data["camel"] for name, data in routing.items()})
    form_fields = {name for name, data in routing.items() if data["form_field"]}
    return swallows, camel, form_fields, probe


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
    swallows, camel, form_fields, probe = _name_evidence(repo_root, args.offline)
    widths = {} if args.offline else control_min_widths(repo_root)
    widths.update({name: tiers["md"] for name, tiers in probe.get("widths_by_size", {}).items() if "md" in tiers})

    if args.artifact:
        targets = [Path(args.artifact)]
    else:
        targets = list(iter_sources(Path(args.directory)))

    violations: list[dict] = []
    for target in targets:
        if not target.is_file():
            print(f"not a file: {target}", file=sys.stderr)
            return 2
        violations.extend(
            check_file(target, widths, _relative(target, repo_root), swallows, form_fields, camel, probe)
        )

    if args.json:
        print(json.dumps({
            "files": len(targets),
            "violations": violations,
            "total": len(violations),
            "control_min_widths": widths,
            "width_source": "installed-ssr" if probe.get("widths_by_size") else ("verified-registry" if widths else "unverified"),
            "name_routing": probe,
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
