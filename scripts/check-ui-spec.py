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

# 8pt-grid spacing tokens from ui-specs (0 allowed for resets).
SPACING_TOKENS = {0, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64}

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
    "ephemeral", "ai-dev-gates",
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


_VALUE_FLAGS = {"--mode": "mode", "--dir": "directory",
                "--ext": "exts", "--exclude": "excludes"}


def _take_value(argv: list, i: int, key: str, state: dict) -> int:
    """Consume a two-argument flag (--mode/--dir/--ext/--exclude) into
    state; returns the next index to read."""
    value = argv[i + 1]
    if key == "mode":
        state["mode"] = value
    elif key == "directory":
        state["directory"] = Path(value)
    elif key == "exts":
        ext = value.lower()
        state["exts"].add(ext if ext.startswith(".") else "." + ext)
    else:
        state["excludes"].add(value)
    return i + 2


def parse_argv(argv: list) -> tuple:
    """Parse the CLI surface into (mode, targets, directory, exts,
    excludes, want_json, report_path)."""
    state = {"mode": "spacing", "directory": None,
             "exts": set(DEFAULT_EXTS), "excludes": set(DEFAULT_EXCLUDES),
             "want_json": False, "report_path": ""}
    targets = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in _VALUE_FLAGS and i + 1 < len(argv):
            i = _take_value(argv, i, _VALUE_FLAGS[arg], state)
            continue
        if arg == "--json":
            state["want_json"] = True
        elif arg == "--all":
            state["mode"] = "all"
        elif arg == "-o" and i + 1 < len(argv):
            state["report_path"] = argv[i + 1]
            i += 1
        else:
            targets.append(arg)
        i += 1
    return (state["mode"], targets, state["directory"], state["exts"],
            state["excludes"], state["want_json"], state["report_path"])


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


def main(argv: list) -> int:
    mode, targets, directory, exts, excludes, want_json, report_path = parse_argv(argv)
    checkers = resolve_checkers(mode)
    paths = collect_targets(targets, directory, exts, excludes)

    violations = []
    for path in paths:
        if not path.exists():
            violations.append(f"{path}: missing target")
            continue
        violations.extend(check_file(path, checkers))

    report = {
        "mode": mode,
        "directory": str(directory) if directory else "",
        "files_scanned": len(paths),
        "violations": [{"file": item.split(":")[0], "detail": item} for item in violations],
        "total": len(violations),
    }
    if want_json or report_path:
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if report_path:
            Path(report_path).parent.mkdir(parents=True, exist_ok=True)
            Path(report_path).write_text(payload + "\n", encoding="utf-8")
        if want_json:
            print(payload)
            # stdout carries ONLY the JSON report; summary goes to stderr
            if violations:
                print(f"UI-SPEC [{mode}]: {len(violations)} violation(s) in "
                      f"{len(paths)} file(s); rejected", file=sys.stderr)
            else:
                print(f"UI-SPEC [{mode}]: OK ({len(paths)} file(s) scanned)",
                      file=sys.stderr)
            return 0 if not violations else 1
    # Violations go to stderr: the runner feeds validator stderr into the
    # retry prompt, so the agent sees exactly what must be fixed.
    for item in violations:
        print(f"UI-SPEC [{mode}] {item}", file=sys.stderr)
    if violations:
        print(f"UI-SPEC [{mode}]: {len(violations)} violation(s) in "
              f"{len(paths)} file(s); rejected", file=sys.stderr)
        return 1
    print(f"UI-SPEC [{mode}]: OK ({len(paths)} file(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
