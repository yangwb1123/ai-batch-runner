#!/usr/bin/env python3
"""Deterministic UI-generation gates for ai-batch-runner (pure stdlib).

The spacing/color/style problems of AI-generated TSX/Dart are NOT code
problems and NOT LLM-capability problems -- they are specification problems.
Instead of asking the model to be careful, these gates mechanically reject
artifacts that violate the UI spec, so the runner's fail-closed retry loop
feeds the violation report back into the agent's next attempt:

    python scripts/check-ui-spec.py --mode spacing   <artifact>
    python scripts/check-ui-spec.py --mode color     <artifact>
    python scripts/check-ui-spec.py --mode style     <artifact>

Modes:
  spacing  reject spacing values outside the token set
           (SizedBox(height:3), EdgeInsets.only(left:13), margin:11 ...)
  color    reject hardcoded hex/rgb/Color(0x...) literals
  style    reject inline styles (style={{...}}, style="margin:...")

Exit code 0 = compliant; 1 = violations found (each printed once).
Register in pi-batch.yaml validators (uispacing/uicolor/uistyle) and attach
to UI generation stages via `validate:`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 8pt-grid spacing tokens from ui-specs (0 allowed for resets).
SPACING_TOKENS = {0, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64}

_SPACING_PATTERNS = [
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
_STYLE_PATTERNS = [
    re.compile(r"style=\{\{"),
    re.compile(r"style=\"[^\"]*(?:margin|padding|font|color|width|height):"),
]


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
                if number in check_number:
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
    return _find_matches(text, _COLOR_PATTERNS, "color")


def check_style(text: str) -> list:
    return _find_matches(text, _STYLE_PATTERNS, "style")


_MODES = {"spacing": check_spacing, "color": check_color, "style": check_style}


def main(argv: list) -> int:
    mode = ""
    targets = []
    for arg in argv:
        if arg == "--mode":
            continue
        if arg in _MODES and not mode:
            mode = arg
        else:
            targets.append(arg)
    if not mode or not targets:
        print("usage: check-ui-spec.py --mode spacing|color|style FILE...", file=sys.stderr)
        return 2
    check = _MODES[mode]
    violations = []
    for name in targets:
        path = Path(name)
        if not path.exists():
            violations.append(f"{path}: missing artifact")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            violations.append(f"{path}: unreadable ({exc})")
            continue
        for item in check(text):
            violations.append(f"{path}: {item}")
    for item in violations:
        print(f"UI-SPEC [{mode}] {item}")
    if violations:
        print(f"UI-SPEC [{mode}]: {len(violations)} violation(s); artifact rejected")
        return 1
    print(f"UI-SPEC [{mode}]: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
