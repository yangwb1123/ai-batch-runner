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
  spacing  reject spacing values outside the 8pt token set and reject
           numeric Iris CSS variables (var(--iris-space-4)); use named
           --iris-space-xxs/... tokens instead
           (SizedBox(height:3), EdgeInsets.only(left:13), margin:11 ...)
  font     reject font sizes outside the typography token set
           (fontSize: 15, font-size: 15px, text-[15px])
  color    reject hardcoded hex/rgb/Color(0x...) literals
  style    reject inline styles (style={{...}}, style="margin:...")
  all      run spacing + color + style

Project scan (--dir) walks the directory recursively, skipping build/vendor
directories by default (node_modules, dist, build, .git, ...). In spacing/all
mode, CSS files are additionally checked for named Iris spacing variables;
source-oriented heuristics remain unchanged for other file types. No frontend
files found -> exit 0, so the gate is safe for repos without frontend code
(a gofmt -l style project gate).

Output: violation details on stderr (the runner feeds validator stderr into
the retry prompt); a human summary on stdout; --json prints a machine
report to stdout and -o writes it to a file.
Exit code 0 = compliant; 1 = violations found.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import ui_tokens

# 8pt-grid spacing tokens 派生自 ui-specs/tokens.json（单一事实源；
# spacing.md §4 的同一集合由 tests/test_ui_geometry.py A2 pinned 测试
# 双向校验）。0 允许用于 reset。tokens.json 缺失/不可解析 → fail closed。
try:
    SPACING_TOKENS = ui_tokens.spacing_tokens()
    TYPOGRAPHY_TOKENS = ui_tokens.typography_tokens()
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
_TAILWIND_SCALE = re.compile(
    r"(?<![\w-])-?(?:m|mx|my|mt|mr|mb|ml|p|px|py|pt|pr|pb|pl|"
    r"gap|space-[xy])-(\d+)(?![\w-])")
_TAILWIND_ARBITRARY = re.compile(
    r"(?<![\w-])-?(?:m|mx|my|mt|mr|mb|ml|p|px|py|pt|pr|pb|pl|"
    r"gap|space-[xy])-\[(\d+)px\](?![\w-])")
_COLOR_PATTERNS = [
    re.compile(r"#[0-9a-fA-F]{3,8}\b"),
    re.compile(r"Color\(0x[0-9a-fA-F]{6,8}\)"),
    re.compile(r"\b(?:rgb|rgba|hsl|hsla)\(\s*\d+"),
]
# Token-definition files are the single source of color constants — the
# literals they define are the tokens, not violations.
_TOKEN_FILE_MARKERS = ("app_colors", "/theme/", "/tokens", "design_tokens")
# Iris exposes named spacing variables only. Numeric-looking variables such
# as var(--iris-space-4) are undefined at computed-value time even though the
# number itself belongs to the 8pt scale, so they need a dedicated check rather
# than the numeric spacing-value check above.
_IRIS_NUMERIC_SPACE_VAR = re.compile(
    r"var\(\s*--iris-space-(\d+)(?:\s*,|\s*\))"
)
_IRIS_SPACE_NAME_BY_NUMBER = {
    1: "xxs",
    2: "xs",
    3: "sm",
    4: "md",
    5: "lg",
    6: "xl",
}

_STYLE_PATTERNS = [
    re.compile(r"style=\{\{"),
    re.compile(r"style=\"[^\"]*(?:margin|padding|font|color|width|height):"),
]
_FONT_PATTERNS = [
    re.compile(r"\bfontSize\s*:\s*[\"']?(\d+)"),
    re.compile(r"\bfont-size\s*:\s*[\"']?(\d+)px?"),
    re.compile(r"\btext-\[(\d+)px\]"),
]

# CSS is scanned separately in project spacing/all mode so the existing
# --ext css contract (full source-oriented checks) remains unchanged.
DEFAULT_EXTS = (".tsx", ".jsx", ".ts", ".js", ".dart", ".vue")
DEFAULT_EXCLUDES = {
    "node_modules", "dist", "build", "out", ".git", ".dart_tool",
    ".next", ".nuxt", "coverage", ".pi-batch", ".venv", "venv",
    "logs", "docs", "__pycache__", "test", "tests", "spec", "e2e",
    "ephemeral", "ai-dev-gates", "vendor", "target",
    ".claude", ".cursor",
}


def _match_with_number_check(pattern, match, label, check_number, token_label,
                             allow_large, number_transform) -> str:
    """Validate one matched token's numeric value against the allowed set."""
    value = match.group(1) if match.lastindex else None
    numbers = re.findall(r"\d+", value or "")
    if not numbers:
        return ""
    number = int(numbers[0])
    checked = number_transform(number, match) if number_transform else number
    if checked in check_number or (allow_large and checked > 128):
        return ""
    return (f"{label}: {match.group(0).strip()} "
            f"(value {number} not in {token_label} "
            f"{sorted(check_number)})")


def _find_matches(text: str, patterns: list, label: str, check_number=None,
                  token_label: str = "spacing tokens", allow_large: bool = True,
                  number_transform=None) -> list:
    """Collect unique violations; optional numeric value validation."""
    found = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            if check_number is not None:
                violation = _match_with_number_check(
                    pattern, match, label, check_number, token_label,
                    allow_large, number_transform)
                if violation:
                    found.append(violation)
            else:
                found.append(f"{label}: {match.group(0).strip()}")
    return sorted(set(found))


def check_iris_spacing(text: str) -> list:
    """Reject numeric Iris CSS spacing variables and suggest their names."""
    violations = []
    for match in _IRIS_NUMERIC_SPACE_VAR.finditer(text):
        number = int(match.group(1))
        name = _IRIS_SPACE_NAME_BY_NUMBER.get(number)
        suggestion = f"--iris-space-{name}" if name else "a named --iris-space-* token"
        violations.append(
            f"spacing: --iris-space-{number} is an undefined numeric Iris "
            f"spacing variable (use {suggestion})"
        )
    return violations


def check_spacing(text: str) -> list:
    violations = _find_matches(text, _SPACING_PATTERNS, "spacing", SPACING_TOKENS)
    violations.extend(_find_matches(text, [_TAILWIND_SCALE], "spacing", SPACING_TOKENS,
                                    number_transform=lambda value, _match: value * 4))
    violations.extend(_find_matches(text, [_TAILWIND_ARBITRARY], "spacing", SPACING_TOKENS))
    violations.extend(check_iris_spacing(text))
    return sorted(set(violations))


def check_color(text: str) -> list:
    """Color literals INSIDE quoted strings (i18n hints, docs) are not
    hardcoded colors — strip string literals before matching."""
    cleaned = re.sub(r"'[^']*'|\"[^\"]*\"", "", text)
    return _find_matches(cleaned, _COLOR_PATTERNS, "color")


def check_style(text: str) -> list:
    return _find_matches(text, _STYLE_PATTERNS, "style")


def check_font(text: str) -> list:
    return _find_matches(text, _FONT_PATTERNS, "font", TYPOGRAPHY_TOKENS,
                         "typography tokens", allow_large=False)


_MODES = {"spacing": check_spacing, "color": check_color, "style": check_style,
          "font": check_font}


def scan_directory(directory: Path, exts: tuple, excludes: set) -> list:
    """Recursively collect frontend source files, skipping vendor/build dirs."""
    if any(part in excludes for part in directory.parts):
        return []
    files = []
    for parent, children, names in os.walk(directory, followlinks=False):
        children[:] = [name for name in children if name not in excludes]
        files.extend(Path(parent) / name for name in names
                     if Path(name).suffix.lower() in exts)
    return sorted(files)


def check_file(path: Path, checkers: list, iris_spacing_only: bool = False) -> list:
    """Violation strings for one file across the active checkers; token
    definition files (app_colors/theme/tokens) are exempt from the color
    check — they are the single source of the tokens. Project-added CSS paths
    can opt into the named Iris spacing-variable check only."""
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
        if iris_spacing_only:
            if label != "spacing":
                continue
            items = check_iris_spacing(text)
        else:
            items = check(text)
        for item in items:
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
        print(f"UI-SPEC: unknown mode '{mode}' (spacing|color|style|font|all)",
              file=sys.stderr)
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


def _scan_paths(paths, checkers, iris_spacing_paths=None):
    violations = []
    iris_spacing_paths = iris_spacing_paths or set()
    for path in paths:
        if not path.exists():
            violations.append(f"{path}: missing target")
            continue
        violations.extend(check_file(path, checkers, path in iris_spacing_paths))
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


def _fail_if_no_scan_target(mode: str, targets: list, directory) -> None:
    """No --dir and no positional file: the gate was never told what to scan.

    This is a **misconfiguration**, not a vacuous pass, and the two must not be
    conflated:

      check-ui-spec.py --all
        -> paths == [] -> violations == [] -> "OK (0 file(s) scanned)", exit 0

    That exit 0 was previously emitted **silently** on stdout/stderr alike (the
    existing _warn_zero_scan only fires when --dir was given), so a hand-run or
    a template that lost its --dir placeholder reads as a clean gate while
    nothing was inspected at all. Agents hit this repeatedly and reported it as
    evidence ("ui-spec 0 violations") — see D211 notes.

    Distinguish the two cases:
      * `--dir X` where X legitimately holds no frontend files -> keep exit 0
        (a gofmt -l style project gate must not fail on a backend repo); the
        existing stderr warning already covers it.
      * no target at all -> fail closed with exit 2, the script's config-error
        code (same class as an unknown --mode or an unparseable token file).
    """
    if directory is not None or targets:
        return
    print("UI-SPEC: error: no scan target. Pass --dir <path> or one or more "
          "files. With neither, nothing is inspected and the run still exits 0 "
          "with a vacuous 'OK' summary — that is an unchecked run, not a result.",
          file=sys.stderr)
    sys.exit(2)


def main(argv: list) -> int:
    parsed = parse_argv(argv)
    mode, targets, directory, exts, excludes = parsed[:5]
    want_json, report_path, baseline_path, update_path = parsed[5:]
    _fail_if_no_scan_target(mode, targets, directory)
    paths = collect_targets(targets, directory, exts, excludes)
    iris_spacing_paths = set()
    if directory is not None and mode in ("spacing", "all") and ".css" not in exts:
        iris_spacing_paths = set(scan_directory(directory, (".css",), excludes))
        paths.extend(sorted(iris_spacing_paths))
    _warn_zero_scan(directory, paths)
    violations = _scan_paths(paths, resolve_checkers(mode), iris_spacing_paths)
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
