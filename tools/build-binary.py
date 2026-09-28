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
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
import binary_manifest

_ASSET_FILES = binary_manifest.ASSET_FILES
_ASSET_DIRS = binary_manifest.ASSET_DIRS


def _machine() -> str:
    value = platform.machine().lower().replace("amd64", "x86_64")
    return {"aarch64": "arm64", "armv8l": "arm64", "x86-64": "x86_64"}.get(value, value)


def binary_label() -> str:
    system = platform.system().lower()
    system = {"darwin": "macos", "windows": "windows"}.get(system, system)
    return f"{system}-{_machine()}"


def _add_data_args(root: Path, snapshot: dict | None = None) -> list[str]:
    """Return platform-correct PyInstaller data mappings."""
    separator = os.pathsep
    values: list[str] = []
    snapshot = snapshot if snapshot is not None else binary_manifest.source_snapshot(root)
    for source, destination in binary_manifest.data_mappings(root, snapshot):
        values.extend(("--add-data", f"{source}{separator}{destination}"))
    return values


def build(output: Path, root: Path = ROOT, artifact_name: str = "pi-batch") -> Path:
    """Build and return the exact executable path without mutating source files."""
    if (not artifact_name or artifact_name in {".", ".."}
            or Path(artifact_name).name != artifact_name):
        raise ValueError("artifact_name must be a simple file name")
    root = root.expanduser().resolve()
    if not root.is_dir() or not (root / "pyproject.toml").is_file():
        raise ValueError(f"source root is not a project: {root}")
    output = output.expanduser().resolve()
    if any(output.is_relative_to(root / name) for name in (*_ASSET_DIRS, "pbatch", "tools")):
        raise ValueError("binary output must not be inside a packaged source directory")
    output.mkdir(parents=True, exist_ok=True)
    name = artifact_name
    if platform.system().lower() == "windows":
        name += ".exe"
    executable = output / name
    with binary_manifest.output_lock(executable):
        _build_locked(root, output, artifact_name, executable)
    return executable


def _build_locked(root: Path, output: Path, artifact_name: str, executable: Path) -> None:
    manifest, license_file = binary_manifest.sidecars(executable)
    # Invalidate old success evidence only after owning the final artifact.
    manifest.unlink(missing_ok=True)
    snapshot = binary_manifest.source_snapshot(root)
    if any(path == root / source for path in (executable, license_file)
           for source in snapshot["files"]):
        raise ValueError("binary output must not overwrite a source input")
    git = binary_manifest.git_record(root, snapshot)
    with tempfile.TemporaryDirectory(prefix="." + executable.name + "-build-", dir=output) as temporary:
        private = Path(temporary)
        candidate = _compile_snapshot(root, private, artifact_name, executable.name, snapshot)
        binary_manifest.write_manifest(candidate, root, snapshot, git, private / "work",
                                       generated_root=private)
        binary_manifest.publish_candidate(candidate, executable, root, snapshot, git)


def _compile_snapshot(root: Path, private: Path, name: str,
                      executable_name: str, snapshot: dict) -> Path:
    staged = private / "source"
    staged.mkdir()
    (private / "dist").mkdir()
    binary_manifest.stage_inputs(root, staged, snapshot)
    command = _build_command(staged, private, name, snapshot)
    subprocess.run(command, cwd=staged, check=True)
    if binary_manifest.source_snapshot(staged) != snapshot:
        raise RuntimeError("staged source inputs changed during binary build")
    candidate = private / "dist" / executable_name
    if not candidate.exists() or binary_manifest.file_record(candidate)["bytes"] <= 0:
        raise RuntimeError(f"PyInstaller did not produce a new executable: {executable_name}")
    if platform.system().lower() != "windows":
        candidate.chmod(candidate.stat().st_mode | 0o111)
    return candidate


def _build_command(root: Path, output: Path, artifact_name: str, snapshot: dict) -> list[str]:
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        # Frozen Python ignores PYTHONUTF8; redirected Chinese output must
        # also work on Windows machines using a legacy ANSI code page.
        "--python-option", "X utf8=1",
        "--name", artifact_name, "--paths", str(root), "--collect-submodules", "pbatch",
        *(_add_data_args(root, snapshot)),
        "--distpath", str(output / "dist"), "--workpath", str(output / "work"),
        "--specpath", str(output / "spec"), str(root / "pi-batch.py"),
    ]
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--name", default="pi-batch")
    args = parser.parse_args(argv)
    try:
        executable = build(args.output, artifact_name=args.name)
    except binary_manifest.BuildBusyError as exc:
        print(str(exc), file=sys.stderr)
        return 5
    print(f"binary: {executable} ({binary_label()})")
    print(f"binding manifest: {binary_manifest.sidecars(executable)[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
