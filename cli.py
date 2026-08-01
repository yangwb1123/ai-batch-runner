#!/usr/bin/env python3
"""cli.py — engineering gate entry point (modeled on snaplink's cli.py).

Usage:
  python cli.py check            Filesize + complexity + root policy + style
  python cli.py check-filesize   File size budget
  python cli.py check-complexity Cyclomatic / cognitive budgets
  python cli.py check-root       Root directory policy
  python cli.py check-style      Whitespace hygiene
  python cli.py check-test       Run the harness's own unit tests
  python cli.py harness          Same as check (pre-push hook target)
"""
from __future__ import annotations

import subprocess
import sys


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess with live output (snaplink cli.py style)."""
    return subprocess.run(list(args), capture_output=False, text=True, check=False, **kwargs)


def _aggregate(checks) -> int:
    """Run checks, sum failure counts, clamp to exit code."""
    total = 0
    for name, fn in checks:
        print(f"-- {name} --")
        try:
            total += int(fn() or 0)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # a broken gate fails the run, never crashes it
            print(f"  ERROR: {name} crashed: {e}")
            total += 1
    return 1 if total > 0 else 0


def cmd_check() -> int:
    return _aggregate([
        ("filesize", _check_filesize),
        ("complexity", _check_complexity),
        ("root-policy", _check_root),
        ("style", _check_style),
    ])


def _check_filesize() -> int:
    from checks.filesize import run as fs_run
    return fs_run()


def _check_complexity() -> int:
    from checks.complexity import run as cx_run
    return cx_run()


def _check_root() -> int:
    from checks.root_files import run as rp_run
    return rp_run()


def _check_style() -> int:
    from checks.style import run as st_run
    return st_run()


def cmd_check_test() -> int:
    """Harness integrity: the gates must verify themselves (zero-dep unittest)."""
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "checks", "-p", "test_*.py"]
    return run(*cmd).returncode


def cmd_harness() -> int:
    return cmd_check()


def main() -> int:
    dispatch = {
        "check": cmd_check,
        "check-filesize": lambda: _aggregate([("filesize", _check_filesize)]),
        "check-complexity": lambda: _aggregate([("complexity", _check_complexity)]),
        "check-root": lambda: _aggregate([("root-policy", _check_root)]),
        "check-style": lambda: _aggregate([("style", _check_style)]),
        "check-test": cmd_check_test,
        "harness": cmd_harness,
    }
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    fn = dispatch.get(sys.argv[1])
    if fn is None:
        print(f"unknown command: {sys.argv[1]}", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main())
