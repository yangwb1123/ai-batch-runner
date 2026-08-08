#!/usr/bin/env python3
"""File size gate: source files must stay under the configured line budget.

Thresholds and ignore patterns live in engineering.yaml (`filesize:`
section) — see checks/config.py. Mirrors snaplink's checks/filesize.py;
only the scanned suffix (.py instead of .go) differs.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checks.config import FilesizeGate, get_config, is_ignored, repo_root, scan_files

_SUFFIX = ".py"


def check_file(path: Path, root: Path, gate: FilesizeGate) -> bool:
    """True when the file passes the line budget (or is exempt)."""
    if path.suffix != _SUFFIX:
        return True
    path = path.resolve()
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    if is_ignored(rel, gate.ignore_patterns):
        return True
    if is_exempt(rel, gate.exemptions):
        return True
    lines = len(path.read_text(encoding="utf-8").splitlines())
    if lines > gate.max_lines:
        print(f"  FAIL: {rel} ({lines} lines, max {gate.max_lines})")
        return False
    return True


def is_exempt(rel: str, exemptions: list) -> bool:
    """Exact path or basename in the (shrinking) debt list."""
    for e in exemptions:
        if e == rel or rel.endswith("/" + e):
            return True
    return False


def run(files: list = None) -> int:
    """Check every .py under the repo root (or the given paths). Returns the
    number of failing files (0 = pass), like snaplink's filesize.run()."""
    root = repo_root()
    gate = get_config().filesize
    if files is None:
        files = scan_files(root, suffix=_SUFFIX)
    failed = 0
    for f in files:
        if not check_file(Path(f), root, gate):
            failed += 1
    if failed:
        print(f"  FILESIZE: {failed} file(s) exceed {gate.max_lines} lines")
    return failed


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
