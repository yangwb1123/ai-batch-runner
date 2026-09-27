#!/usr/bin/env python3
"""Build one self-contained ``pi-batch`` executable with PyInstaller.

The binary contains the orchestrator and its read-only rule assets; provider
CLIs (pi/codex/claude/...) remain explicitly external runtime dependencies.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_ASSET_FILES = ("pi-batch.py", "pi-batch.yaml", "quality.py", "pyproject.toml")
_ASSET_DIRS = (
    "scripts", "evals", "examples", "agent-engineering", "pipelines", "prompts",
    "product-specs", "ui-specs", "backend-specs", "evolution-specs", "docs",
)


def _machine() -> str:
    value = platform.machine().lower().replace("amd64", "x86_64")
    return {"aarch64": "arm64", "armv8l": "arm64", "x86-64": "x86_64"}.get(value, value)


def binary_label() -> str:
    system = platform.system().lower()
    system = {"darwin": "macos", "windows": "windows"}.get(system, system)
    return f"{system}-{_machine()}"


def _add_data_args(root: Path) -> list[str]:
    """Return platform-correct PyInstaller data mappings."""
    separator = os.pathsep
    values: list[str] = []
    for name in _ASSET_FILES:
        source = root / name
        if source.is_file():
            values.extend(("--add-data", f"{source}{separator}share/ai-batch-runner"))
    for name in _ASSET_DIRS:
        source = root / name
        if source.is_dir():
            values.extend(("--add-data", f"{source}{separator}share/ai-batch-runner/{name}"))
    # AST-based quality/graph checks need real source, not an empty PYZ tree.
    # Keep only authored package files, excluding bytecode and runtime state.
    for source in sorted((root / "pbatch").glob("*")):
        if source.is_file() and source.suffix in {".py", ".md", ".yaml"}:
            values.extend(("--add-data", f"{source}{separator}share/ai-batch-runner/pbatch"))
    return values


def build(output: Path, root: Path = ROOT, artifact_name: str = "pi-batch") -> Path:
    """Build and return the exact executable path without mutating source files."""
    if (not artifact_name or artifact_name in {".", ".."}
            or Path(artifact_name).name != artifact_name):
        raise ValueError("artifact_name must be a simple file name")
    if not root.is_dir() or not (root / "pyproject.toml").is_file():
        raise ValueError(f"source root is not a project: {root}")
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = artifact_name
    if platform.system().lower() == "windows":
        name += ".exe"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        # Frozen Python ignores PYTHONUTF8; redirected Chinese output must
        # also work on Windows machines using a legacy ANSI code page.
        "--python-option", "X utf8=1",
        "--name", artifact_name, "--paths", str(root), "--collect-submodules", "pbatch",
        "--collect-data", "pbatch", *(_add_data_args(root)),
        "--distpath", str(output), "--workpath", str(output / ".build"),
        "--specpath", str(output / ".spec"), str(root / "pi-batch.py"),
    ]
    subprocess.run(command, cwd=root, check=True)
    executable = output / name
    if not executable.is_file() or executable.stat().st_size <= 0:
        raise RuntimeError(f"PyInstaller did not produce {executable}")
    if platform.system().lower() != "windows":
        executable.chmod(executable.stat().st_mode | 0o111)
    return executable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--name", default="pi-batch")
    args = parser.parse_args(argv)
    executable = build(args.output, artifact_name=args.name)
    print(f"binary: {executable} ({binary_label()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
