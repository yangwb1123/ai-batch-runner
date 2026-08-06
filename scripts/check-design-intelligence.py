#!/usr/bin/env python3
"""Design Intelligence gate (pure stdlib).

Deterministic half of the "认知驱动设计" checks — catches the flat
"database dump" anti-pattern before a human/LLM review:

1. KPI emphasis  — large numbers (>=28px / headline styles) should exist
   where the page shows metrics; guards against all-equal-weight layouts.
2. Semantic color usage — status colors must come from tokens; raw
   hex/rgb literals for green/red/amber are violations (the token gate
   complements check-ui-spec's generic color scan).
3. Empty state guidance — EmptyState/EmptyHint usage should carry an
   action label (teaching empty states), not bare "No data".
4. Dual encoding — status displays should pair color with icon/text
   (looks for StatusChip/Badge usage as the canonical dual-encoded
   component).

Scans a directory tree of Dart/Vue/TS files (stdlib only). Exits 0 when
clean, 1 with violations, 2 on usage errors. `--json` emits a report.

Usage:
  python check-design-intelligence.py [--dir DIR] [--json] [-o OUT]
  python check-design-intelligence.py --selfcheck
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Deterministic checks
# --------------------------------------------------------------------------

# Semantic status hexes that must not appear as raw literals (light mode).
_SEMANTIC_HEX = re.compile(
    r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})",
)
_SEMANTIC_NAMES = {
    "green", "red", "amber", "orange",
    "0x4caf50", "0x4CAF50", "0xf44336", "0xF44336", "0xff9800", "0xFF9800",
    "0x10b981", "0x10B981", "0xef4444", "0xEF4444", "0xf59e0b", "0xF59E0B",
    "0x059669", "0x059669", "0xb91c1c", "0xB91C1C", "0xd97706", "0xD97706",
}
_RAW_COLOR_CALL = re.compile(
    r"(?:Color|Colors|color)\(0x[0-9a-fA-F]{6,8}\)",
)
# Known semantic colors that are allowed when they come from token
# definitions themselves (app_colors/tokens files).
_TOKEN_FILES = re.compile(
    r"(app_colors|tokens|theme|palette|design_token)",
    re.IGNORECASE,
)

# KPI emphasis styles: fontSize >= 28 or headline/display styles.
_KPI_STYLE = re.compile(
    r"fontSize:\s*(?:2[89]|[3-9]\d)\.?\d*"
    r"|headlineMedium|headlineSmall|displaySmall|displayMedium",
)
# A "metric-ish" label near the number (contextual hint for KPI detection).
_METRIC_HINT = re.compile(
    r"(rate|percent|达成|完成率|金额|score|health|健康|指标|余额|"
    r"progress|usage|KPI|达成率|点击率|转化|\d\s*%)",
    re.IGNORECASE,
)

# Empty states should teach (have an action), not just say "no data".
_EMPTY_COMPONENT = re.compile(
    r"EmptyState\(|EmptyHint\(|_empty\(|Empty\(",
)
_EMPTY_WITH_ACTION = re.compile(
    r"EmptyState\([\s\S]{0,400}?actionLabel|EmptyState\([\s\S]{0,400}?onAction",
)
_BARE_EMPTY_TEXT = re.compile(
    r"[Nn]o\s+\w+\s+(?:yet|here|found|registered|available)|"
    r"暂无数据|没有数据|No data",
)

# Dual encoding: status chips / badges as the canonical pattern.
_DUAL_ENCODED = re.compile(
    r"StatusChip\.?\w*\(|Badge\(|_Chip\(|Chip\(",
)
_STATUS_TEXT_ONLY = re.compile(
    r"""["'](Active|Inactive|Suspended|Healthy|Unhealthy|成功|失败|正常|异常)["']""",
)


_SEMANTIC_HEXES = {
    c.lower()
    for c in (
        "Color(0x4caf50)", "Color(0x10b981)", "Color(0x059669)",
        "Color(0xf44336)", "Color(0xef4444)", "Color(0xb91c1c)",
        "Color(0xff9800)", "Color(0xf59e0b)", "Color(0xd97706)",
    )
}


def _check_semantic_colors(rel: str, source: str) -> list[dict]:
    """Raw semantic color literals outside token files."""
    if _TOKEN_FILES.search(rel):
        return []
    findings = []
    for match in _RAW_COLOR_CALL.finditer(source):
        value = match.group(0)
        if value.lower() in _SEMANTIC_HEXES:
            findings.append({
                "file": rel,
                "check": "semantic_color_literal",
                "detail": (
                    f"{rel}: raw semantic color {value} — use a "
                    "semantic token (success/warning/danger/info)"
                ),
            })
    for match in re.finditer(r"\bColors\.(green|red|orange|amber)\b", source):
        findings.append({
            "file": rel,
            "check": "semantic_color_literal",
            "detail": f"{rel}: Colors.{match.group(1)} — use semantic tokens",
        })
    return findings


def _is_kpi_page(rel: str) -> bool:
    """Workbench/overview-class pages only."""
    if "/widgets/" in rel:
        return False
    is_page = bool(
        re.search(r"/(screens?|views?|pages?)/", rel)
        or re.search(r"(?:^|_)(?:screen|tab|page|view)s?\.dart$", rel.lower())
    )
    if not is_page:
        return False
    return bool(
        re.search(
            r"(overview|dashboard|workbench|stats|statistics|analytics|"
            r"metrics|summary|landing)",
            Path(rel).name.lower(),
        )
    )


def _check_kpi(rel: str, source: str) -> list[dict]:
    """KPI emphasis: workbench pages with metrics need big-number style."""
    if not _is_kpi_page(rel) or "Widget build" not in source:
        return []
    body = "\n".join(
        line for line in source.splitlines() if not line.startswith("import")
    )
    if not _METRIC_HINT.search(body) or _KPI_STYLE.search(body):
        return []
    # Navigation shells and componentized KPIs are not workbenches.
    if "NavigationRail" in source:
        return []
    if re.search(r"StatCard|CountUp|_Stat|_Metric|_Kpi", source):
        return []
    return [{
        "file": rel,
        "check": "kpi_emphasis_missing",
        "detail": (
            f"{rel}: metrics present but no emphasized KPI style — "
            "the most important number should be large (headline/28px+)"
        ),
    }]


def _check_empty_state(rel: str, source: str) -> list[dict]:
    """Empty states should teach (have an action), not be bare."""
    if "Widget build" not in source:
        return []
    if not _EMPTY_COMPONENT.search(source) or not _BARE_EMPTY_TEXT.search(source):
        return []
    for m in _EMPTY_COMPONENT.finditer(source):
        chunk = source[m.start():m.start() + 600]
        if _EMPTY_WITH_ACTION.search(chunk):
            return []
    return [{
        "file": rel,
        "check": "empty_state_no_action",
        "detail": (
            f"{rel}: empty state without action — empty states "
            "should teach the next step (actionLabel/onAction)"
        ),
    }]


def _check_dual_encoding(rel: str, source: str) -> list[dict]:
    """Status displays must be dual-encoded (chip/badge + text)."""
    if "Widget build" not in source:
        return []
    if not _STATUS_TEXT_ONLY.search(source) or _DUAL_ENCODED.search(source):
        return []
    if re.search(r"Radio|Segmented|Dropdown|ChoiceChip|SwitchListTile|Switch\(", source):
        return []
    return [{
        "file": rel,
        "check": "status_not_dual_encoded",
        "detail": (
            f"{rel}: bare status text without a chip/badge component — "
            "status must be dual-encoded (color + icon/text)"
        ),
    }]


def _check_file(path: Path, source: str) -> list[dict]:
    """One file -> violations. Pure heuristics; no AST (stdlib only)."""
    rel = str(path)
    findings: list[dict] = []
    findings.extend(_check_semantic_colors(rel, source))
    findings.extend(_check_kpi(rel, source))
    findings.extend(_check_empty_state(rel, source))
    findings.extend(_check_dual_encoding(rel, source))
    return findings


def _collect_files(root: Path, skip: str) -> list[Path]:
    """Source files under root, honoring skip fragments."""
    skipped = [s for s in skip.split(",") if s]
    return [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in {".dart", ".ts", ".tsx", ".vue", ".js", ".jsx"}
        and not any(part in skipped for part in p.parts)
        and ".dart_tool" not in p.parts
        and "node_modules" not in p.parts
        and "build" not in p.parts
    ]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _selfcheck() -> int:
    bad = 0
    ui = "class X extends StatelessWidget { Widget build(BuildContext context) {"
    # Status text without any chip/badge component -> violation.
    status_only = ui + " Text('Active'); Text('Suspended') }"
    if not _check_file(Path("sample.dart"), status_only):
        bad += 1
        print("selfcheck FAIL: bare status text should violate")
    # Bare empty text inside an EmptyState without action -> violation.
    empty_bare = ui + " return EmptyState(title: 'No data'); }"
    if not _check_file(Path("sample.dart"), empty_bare):
        bad += 1
        print("selfcheck FAIL: bare empty state should violate")
    # Dual-encoded file is clean.
    clean = ui + " StatusChip.active(); Text('Active') }"
    if _check_file(Path("sample.dart"), clean):
        bad += 1
        print("selfcheck FAIL: dual-encoded file should be clean")
    # KPI emphasis present -> clean.
    kpi_ok = (
        ui
        + " Text('98%', style: TextStyle(fontSize: 32, fontWeight: FontWeight.w700)) }"
    )
    if _check_file(Path("sample.dart"), kpi_ok):
        bad += 1
        print("selfcheck FAIL: emphasized KPI should be clean")
    print("selfcheck:", "OK" if bad == 0 else f"{bad} FAILURES")
    return 0 if bad == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=".", help="directory to scan")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("-o", "--output", help="write report to file")
    parser.add_argument("--selfcheck", action="store_true")
    parser.add_argument(
        "--skip",
        default="",
        help="comma-separated path fragments to skip (.dart_tool,test,...)",
    )
    args = parser.parse_args(argv)

    if args.selfcheck:
        return _selfcheck()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"error: {args.dir} is not a directory", file=sys.stderr)
        return 2

    files = _collect_files(root, args.skip)
    violations: list[dict] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        violations.extend(_check_file(path, source))

    report = {
        "directory": args.dir,
        "files_scanned": len(files),
        "violations": violations,
        "total": len(violations),
    }

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for v in violations:
            print(v["detail"])
        print(f"{len(violations)} violation(s) found")

    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
