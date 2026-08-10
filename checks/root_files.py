#!/usr/bin/env python3
"""Root directory policy: the repo root stays a thin launcher layer.

Only allowed files may live at the root (config, entry points, docs), and
banned patterns (e.g. tests outside tests/checks/) are rejected. Mirrors
snaplink's checks/root_files.py policy section.
"""
from __future__ import annotations

import fnmatch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checks.config import RootPolicyGate, get_config, repo_root


def check_root(root: Path, gate: RootPolicyGate) -> int:
    """Return the number of violations at the root level."""
    # Linked Git worktrees represent repository metadata with a root-level
    # ``.git`` gitfile rather than a directory; neither form is product code.
    files = [p for p in root.iterdir() if p.is_file() and p.name != ".git"]
    failures = 0
    for f in sorted(files):
        name = f.name
        allowed = name in gate.allowed_files
        banned = any(fnmatch.fnmatch(name, p) for p in gate.banned_patterns)
        if not allowed and not banned:
            print(f"  FAIL: unexpected root file {name} (add to allowed_files or move it)")
            failures += 1
        elif banned:
            print(f"  FAIL: banned root file {name} (matches {gate.banned_patterns})")
            failures += 1
    if len(files) > gate.max_files:
        print(f"  FAIL: {len(files)} root files, max {gate.max_files}")
        failures += 1
    return failures


def run() -> int:
    gate = get_config().root_policy
    failed = check_root(repo_root(), gate)
    if failed:
        print(f"  ROOT POLICY: {failed} violation(s) (max {gate.max_files} files)")
    return failed


if __name__ == "__main__":
    sys.exit(run())
