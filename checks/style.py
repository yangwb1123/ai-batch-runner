#!/usr/bin/env python3
"""Style gate: whitespace hygiene that is cheap to enforce without a linter.

Fails on: trailing whitespace, missing final newline, tab indentation,
mixed-encoding hazards. Warns (non-fatal) on lines over the configured
length. The upstream-synced files are exempt via engineering.yaml.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checks.config import StyleGate, get_config, is_ignored, repo_root, scan_files

_SUFFIX = ".py"


def check_text(text: str, rel: str, gate: StyleGate) -> int:
    """Check file content; returns number of hard violations."""
    if is_ignored(rel, gate.ignore_patterns):
        return 0
    failed = 0
    for i, line in enumerate(text.splitlines(), 1):
        if line.endswith((" ", "\t")):
            print(f"  FAIL: {rel}:{i} trailing whitespace")
            failed += 1
        if "\t" in line[: len(line) - len(line.lstrip())]:
            print(f"  FAIL: {rel}:{i} tab indentation")
            failed += 1
        if len(line) > gate.max_line_length:
            print(f"  WARN: {rel}:{i} {len(line)} chars (max {gate.max_line_length})")
    if text and not text.endswith("\n"):
        print(f"  FAIL: {rel} missing final newline")
        failed += 1
    return failed


def check_file(path: Path, root: Path, gate: StyleGate) -> int:
    if path.suffix != _SUFFIX:
        return 0
    path = path.resolve()
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        print(f"  FAIL: {rel} not utf-8 ({e})")
        return 1
    return check_text(text, rel, gate)


def run(files: list = None) -> int:
    """Hard violation count (0 = pass); long-line warnings are non-fatal."""
    root = repo_root()
    gate = get_config().style
    if files is None:
        files = scan_files(root, suffix=_SUFFIX)
    failed = 0
    for f in files:
        failed += check_file(Path(f), root, gate)
    if failed:
        print(f"  STYLE: {failed} hard violation(s)")
    return failed


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
