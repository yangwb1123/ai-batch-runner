#!/usr/bin/env python3
"""Fail-closed release metadata checks for ai-batch-runner."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXPECTED_DISTRIBUTION = "ai-batch-runner"
EXPECTED_ENTRY_POINT = "pbatch.cli:main"
STABLE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
UNCLEARED_LICENSE = "must be re-checked for compatibility before further distribution"


def _project_value(text: str, key: str) -> str:
    project = re.search(r"\[project\](.*?)(?=\n\[|\Z)", text, re.S)
    if project is None:
        return ""
    match = re.search(
        rf'^\s*{re.escape(key)}\s*=\s*"([^"]+)"', project.group(1), re.M
    )
    return match.group(1) if match else ""


def _table_value(text: str, table: str, key: str) -> str:
    """Read one simple quoted value from a named TOML table.

    Release metadata uses plain quoted scalars. Scoping the match to the
    exact table prevents comments or unrelated tables from satisfying a
    public command contract.
    """
    section = re.search(
        rf"^\[{re.escape(table)}\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S
    )
    if section is None:
        return ""
    match = re.search(
        rf'^\s*{re.escape(key)}\s*=\s*"([^"]+)"\s*(?:#.*)?$',
        section.group(1), re.M,
    )
    return match.group(1) if match else ""


def release_errors(tag: str = "", root: Path = ROOT) -> list[str]:
    """Return release-policy violations for *root* without mutating it."""
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    name = _project_value(pyproject, "name")
    version = _project_value(pyproject, "version")
    errors = []
    if name != EXPECTED_DISTRIBUTION:
        errors.append(
            f"project name must be {EXPECTED_DISTRIBUTION!r}, got {name!r}"
        )
    if STABLE_VERSION.fullmatch(version) is None:
        errors.append(f"project version must be stable X.Y.Z, got {version!r}")
    entry_point = _table_value(pyproject, "project.scripts", "pi-batch")
    if entry_point != EXPECTED_ENTRY_POINT:
        errors.append(
            "console entry point pi-batch must target "
            f"{EXPECTED_ENTRY_POINT!r}, got {entry_point!r}"
        )
    if f"## [{version}]" not in changelog:
        errors.append(f"CHANGELOG.md has no release heading for {version}")
    if tag and tag != f"v{version}":
        errors.append(f"tag {tag!r} does not match project version v{version}")
    if tag and UNCLEARED_LICENSE in license_text:
        errors.append(
            "tagged publication blocked: upstream snaplink/ai-dev license "
            "clearance is unresolved in LICENSE"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="", help="require vX.Y.Z to match the package")
    args = parser.parse_args(argv)
    errors = release_errors(args.tag)
    if errors:
        for error in errors:
            print(f"release-check: FAIL: {error}", file=sys.stderr)
        return 1
    version = _project_value(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), "version"
    )
    print(f"release-check: PASS: {EXPECTED_DISTRIBUTION} {version} -> pi-batch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
