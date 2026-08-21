"""Single runtime version source for source checkouts and installed wheels."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as metadata_version
from pathlib import Path


def _source_version() -> str:
    path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version ="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _package_version() -> str:
    try:
        return metadata_version("ai-batch-runner")
    except PackageNotFoundError:
        return ""


VERSION = _source_version() or _package_version() or "0.0.0-dev"
