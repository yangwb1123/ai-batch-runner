#!/usr/bin/env python3
"""Deterministic UI-generation and project-scan gates (pure stdlib).

The spacing/color/style problems of AI-generated TSX/Dart are NOT code
problems and NOT LLM-capability problems -- they are specification problems.
Instead of asking the model to be careful, these gates mechanically reject
artifacts (or whole projects) that violate the UI spec, and the runner's
fail-closed retry loop feeds the violation report (stderr) back into the
agent's next attempt:

    python scripts/check-ui-spec.py --mode spacing   <artifact>       # single file
    python scripts/check-ui-spec.py --mode color     <artifact>
    python scripts/check-ui-spec.py --mode style     <artifact>
    python scripts/check-ui-spec.py --dir src --all                    # project scan
    python scripts/check-ui-spec.py --dir src --all --json -o report.json

Modes:
  spacing  reject spacing values outside the 8pt token set
           (SizedBox(height:3), EdgeInsets.only(left:13), margin:11 ...)
  color    reject hardcoded hex/rgb/Color(0x...) literals
  style    reject inline styles (style={{...}}, style="margin:...")
  all      run spacing + color + style

Project scan (--dir) walks the directory recursively, skipping build/vendor
directories by default (node_modules, dist, build, .git, ...). No frontend
files found -> exit 0, so the gate is safe for repos without frontend code
(a gofmt -l style project gate).

Output: violation details on stderr (the runner feeds validator stderr into
the retry prompt); a human summary on stdout; --json prints a machine
report to stdout and -o writes it to a file.
Exit code 0 = compliant; 1 = violations found.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import ui_tokens

# 8pt-grid spacing tokens 派生自 ui-specs/tokens.json（单一事实源；
# spacing.md §4 的同一集合由 tests/test_ui_geometry.py A2 pinned 测试
# 双向校验）。0 允许用于 reset。tokens.json 缺失/不可解析 → fail closed。
try:
    SPACING_TOKENS = ui_tokens.spacing_tokens()
except ui_tokens.TokenLoadError as exc:
    print(f"UI-SPEC: {exc}", file=sys.stderr)
    sys.exit(2)

_SPACING_PATTERNS = [
    # spacing sizes; big values (>128, matched below) are component dims
    re.compile(r"SizedBox\((?:width|height):\s*(\d+)"),
    re.compile(r"EdgeInsets\.(?:all|only|symmetric|fromLTRB)\(([^)]*)\)"),
    re.compile(r"\b(?:margin|padding|gap|rowGap|columnGap):\s*(\d+)"),
    re.compile(r"\b(?:margin|padding)(?:Left|Right|Top|Bottom):\s*(\d+)"),
    re.compile(r"style=\{\{?\s*(?:margin|padding):\s*(\d+)"),
]
_COLOR_PATTERNS = [
    re.compile(r"#[0-9a-fA-F]{3,8}\b"),
    re.compile(r"Color\(0x[0-9a-fA-F]{6,8}\)"),
    re.compile(r"\b(?:rgb|rgba|hsl|hsla)\(\s*\d+"),
]
# Token-definition files are the single source of color constants — the
# literals they define are the tokens, not violations.
_TOKEN_FILE_MARKERS = ("app_colors", "/theme/", "/tokens", "design_tokens")
_STYLE_PATTERNS = [
    re.compile(r"style=\{\{"),
    re.compile(r"style=\"[^\"]*(?:margin|padding|font|color|width|height):"),
]

DEFAULT_EXTS = (".tsx", ".jsx", ".ts", ".js", ".dart", ".vue")
DEFAULT_EXCLUDES = {
    "node_modules", "dist", "build", "out", ".git", ".dart_tool",
    ".next", ".nuxt", "coverage", ".pi-batch", ".venv", "venv",
    "logs", "docs", "__pycache__", "test", "tests", "spec", "e2e",
    "ephemeral", "ai-dev-gates", "vendor", "target",
    ".claude", ".cursor",
}


def _find_matches(text: str, patterns: list, label: str, check_number=None) -> list:
    """Collect unique violations; optional numeric value validation."""
    found = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            value = match.group(1) if match.lastindex else None
            if check_number is not None:
                numbers = re.findall(r"\d+", value or "")
                if not numbers:
                    continue
                number = int(numbers[0])
                if number in check_number or number > 128:
                    continue
                found.append(f"{label}: {match.group(0).strip()} "
                             f"(value {number} not in spacing tokens "
                             f"{sorted(check_number)})")
            else:
                found.append(f"{label}: {match.group(0).strip()}")
    return sorted(set(found))


def check_spacing(text: str) -> list:
    return _find_matches(text, _SPACING_PATTERNS, "spacing", SPACING_TOKENS)


def check_color(text: str) -> list:
    """Color literals INSIDE quoted strings (i18n hints, docs) are not
    hardcoded colors — strip string literals before matching."""
    cleaned = re.sub(r"'[^']*'|\"[^\"]*\"", "", text)
    return _find_matches(cleaned, _COLOR_PATTERNS, "color")


def check_style(text: str) -> list:
    return _find_matches(text, _STYLE_PATTERNS, "style")


_MODES = {"spacing": check_spacing, "color": check_color, "style": check_style}


def scan_directory(directory: Path, exts: tuple, excludes: set) -> list:
    """Recursively collect frontend source files, skipping vendor/build dirs."""
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_dir():
            continue
        if any(part in excludes for part in path.parts[:-1]):
            continue
        if path.suffix.lower() in exts:
            files.append(path)
    return files


def check_file(path: Path, checkers: list) -> list:
    """Violation strings for one file across the active checkers; token
    definition files (app_colors/theme/tokens) are exempt from the color
    check — they are the single source of the tokens."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"{path}: unreadable ({exc})"]
    rel = str(path)
    is_token_file = any(marker in rel for marker in _TOKEN_FILE_MARKERS)
    violations = []
    for label, check in checkers:
        if is_token_file and label == "color":
            continue
        for item in check(text):
            violations.append(f"{path}: {item}")
    return violations


_VALUE_FLAGS = {
    "--mode": "mode", "--dir": "directory", "--ext": "exts",
    "--exclude": "excludes", "-o": "report_path",
    "--baseline": "baseline_path", "--update-baseline": "update_path",
}
_SWITCH_FLAGS = {"--json": ("want_json", True), "--all": ("mode", "all")}


def _take_value(argv: list, i: int, key: str, state: dict) -> int:
    """Consume a two-argument flag (--mode/--dir/--ext/--exclude) into
    state; returns the next index to read."""
    value = argv[i + 1]
    if key == "directory":
        state["directory"] = Path(value)
    elif key == "exts":
        ext = value.lower()
        state["exts"].add(ext if ext.startswith(".") else "." + ext)
    elif key == "excludes":
        state["excludes"].add(value)
    else:
        state[key] = value
    return i + 2


def parse_argv(argv: list) -> tuple:
    """Parse the CLI surface into (mode, targets, directory, exts,
    excludes, want_json, report_path)."""
    state = {"mode": "spacing", "directory": None,
             "exts": set(DEFAULT_EXTS), "excludes": set(DEFAULT_EXCLUDES),
             "want_json": False, "report_path": "",
             "baseline_path": "", "update_path": ""}
    targets = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in _VALUE_FLAGS and i + 1 < len(argv):
            i = _take_value(argv, i, _VALUE_FLAGS[arg], state)
            continue
        if arg in _SWITCH_FLAGS:
            key, value = _SWITCH_FLAGS[arg]
            state[key] = value
        else:
            targets.append(arg)
        i += 1
    return (state["mode"], targets, state["directory"], state["exts"],
            state["excludes"], state["want_json"], state["report_path"],
            state["baseline_path"], state["update_path"])


def resolve_checkers(mode: str) -> list:
    """(label, callable) pairs for the requested mode; all = every checker."""
    if mode == "all":
        return sorted(_MODES.items())
    if mode not in _MODES:
        print(f"UI-SPEC: unknown mode '{mode}' (spacing|color|style|all)", file=sys.stderr)
        sys.exit(2)
    return [(mode, _MODES[mode])]


def collect_targets(targets: list, directory: Path, exts: tuple, excludes: set) -> list:
    """Positional files plus any --dir scan; missing files are violations."""
    paths = [Path(t) for t in targets]
    if directory is not None:
        paths.extend(scan_directory(directory, exts, excludes))
    return paths


def _load_baseline(path):
    """Load a baseline report JSON: {violations: [{file, detail}]}. Absent or
    malformed baseline fails closed (no violations are grandfathered)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = data.get("violations", []) if isinstance(data, dict) else []
        return {(str(item.get("file", "")), str(item.get("detail", "")))
                for item in items if isinstance(item, dict)}
    except (OSError, ValueError):
        return set()


def _warn_zero_scan(directory, paths: list) -> None:
    """Project scan with zero frontend files passes vacuously; make it
    visible on stderr (the runner's retry-feedback channel) while keeping
    exit 0 — a config hint (check ui_scan_dir), not a failure.

    Known benign false positive: a project whose frontend files are all
    excluded by --exclude scans 0 files and hits this warning despite
    having frontend code — harmless (a hint, not a gate failure)."""
    if directory is None or paths:
        return
    print(f"UI-SPEC: warning: 0 file(s) scanned under --dir {directory}; "
          f"no frontend files found - gate passes vacuously "
          f"(check ui_scan_dir in pi-batch.yaml)", file=sys.stderr)


def _write_baseline(path, violations):
    """Persist the current full violation set as the baseline snapshot."""
    payload = json.dumps(
        {"violations": [{"file": item.split(":")[0], "detail": item}
                        for item in violations]},
        ensure_ascii=False, indent=2)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(payload + "\n", encoding="utf-8")


def _apply_baseline(violations, baseline_path, update_path):
    """Ratchet semantics: only violations NOT in the baseline fail;
    --update-baseline persists the full set (no fail in that mode)."""
    if update_path:
        _write_baseline(update_path, violations)
        return [], 0
    if not baseline_path:
        return violations, len(violations)
    baseline = _load_baseline(baseline_path)
    remaining = [v for v in violations
                 if (v.split(":")[0], v) not in baseline]
    return remaining, len(remaining)


def _scan_paths(paths, checkers):
    violations = []
    for path in paths:
        if not path.exists():
            violations.append(f"{path}: missing target")
            continue
        violations.extend(check_file(path, checkers))
    return violations


def _report_data(mode, directory, paths, violations, baseline_path,
                 remaining, new_count):
    return {
        "mode": mode,
        "directory": str(directory) if directory else "",
        "files_scanned": len(paths),
        "violations": [{"file": item.split(":")[0], "detail": item} for item in violations],
        "total": len(violations),
        "baseline": bool(baseline_path),
        "new_violations": [{"file": item.split(":")[0], "detail": item}
                           for item in remaining],
        "new_total": new_count,
    }


def _emit_json_report(report, want_json, report_path):
    if want_json or report_path:
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if report_path:
            Path(report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(report_path).write_text(payload + "\n", encoding="utf-8")
        if want_json:
            print(payload)


def _summary(mode, paths, violations, remaining, baseline_path):
    if remaining:
        label = "new violation(s)" if baseline_path else "violation(s)"
        return (f"UI-SPEC [{mode}]: {len(remaining)} {label} in "
                f"{len(paths)} file(s); rejected"), 1
    if violations and baseline_path:
        return (f"UI-SPEC [{mode}]: OK ({len(violations)} baseline, "
                "0 new; ratchet held)"), 0
    return f"UI-SPEC [{mode}]: OK ({len(paths)} file(s) scanned)", 0


def _finish_report(mode, paths, violations, remaining, baseline_path, want_json):
    if not want_json:
        for item in remaining:
            print(f"UI-SPEC [{mode}] {item}", file=sys.stderr)
    message, code = _summary(mode, paths, violations, remaining, baseline_path)
    stream = sys.stderr if want_json or code else sys.stdout
    print(message, file=stream)
    return code


def main(argv: list) -> int:
    parsed = parse_argv(argv)
    mode, targets, directory, exts, excludes = parsed[:5]
    want_json, report_path, baseline_path, update_path = parsed[5:]
    paths = collect_targets(targets, directory, exts, excludes)
    _warn_zero_scan(directory, paths)
    violations = _scan_paths(paths, resolve_checkers(mode))
    remaining, new_count = _apply_baseline(violations, baseline_path, update_path)
    report = _report_data(
        mode, directory, paths, violations, baseline_path, remaining, new_count,
    )
    _emit_json_report(report, want_json, report_path)
    return _finish_report(
        mode, paths, violations, remaining, baseline_path, want_json,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
