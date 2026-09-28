#!/usr/bin/env python3
"""Bind a local binary to its source inputs; this is not a readiness certificate."""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile


ASSET_FILES = ("pi-batch.py", "pi-batch.yaml", "quality.py", "pyproject.toml", "LICENSE")
ASSET_DIRS = (
    "scripts", "evals", "examples", "agent-engineering", "pipelines", "prompts",
    "product-specs", "ui-specs", "backend-specs", "evolution-specs", "docs",
)
RECIPE_FILES = ("tools/build-binary.py", "tools/binary_manifest.py")
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "dist", "build", ".build", ".spec", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".venv", "node_modules",
})
SCOPE = "artifact and source binding only; no test, production readiness or license clearance claim"


class BuildBusyError(RuntimeError):
    """A cooperating builder already owns the same final artifact."""


def file_record(path: Path) -> dict:
    """Hash a regular, non-symlink file without loading it all into memory."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"build input must be a regular non-symlink file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    current = path.lstat()
    if any(getattr(before, key) != getattr(item, key) for key in keys for item in (after, current)):
        raise RuntimeError(f"build input changed while hashing: {path}")
    return {"sha256": digest.hexdigest(), "bytes": after.st_size}


def _tree(root: Path, name: str, visible: set[str] | None) -> list[str]:
    base = root / name
    if not base.exists() and not base.is_symlink():
        return []
    if base.is_symlink() or not base.is_dir():
        raise ValueError(f"build input directory must not be a symlink: {base}")
    files = []
    for parent, children, names in os.walk(base, followlinks=False):
        directory = Path(parent)
        children[:] = sorted(child for child in children if child not in IGNORED_DIRS)
        if visible is not None:
            prefix = directory.relative_to(root).as_posix() + "/"
            children[:] = [child for child in children
                           if any(path.startswith(prefix + child + "/") for path in visible)]
        if any((directory / child).is_symlink() for child in children):
            raise ValueError(f"build input contains a symlink directory: {directory}")
        candidates = [(directory / item).relative_to(root).as_posix() for item in sorted(names)
                      if not item.endswith((".pyc", ".pyo"))]
        files.extend(path for path in candidates if visible is None or path in visible)
    return files


def source_snapshot(root: Path) -> dict:
    """Use the same enumerated inputs for the manifest and PyInstaller mappings."""
    root = root.resolve()
    listing = (_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
               if _is_git_root(root) else None)
    visible = set(os.fsdecode(listing).split("\0")) if listing is not None else None
    files = list(ASSET_FILES + RECIPE_FILES)
    for name in (*ASSET_DIRS, "pbatch"):
        files.extend(_tree(root, name, visible))
    directories = {parent.as_posix() for name in files for parent in Path(name).parents
                   if parent != Path(".")}
    if "pbatch" not in directories:
        raise ValueError("source root requires a pbatch package")
    return {"files": {name: file_record(root / name) for name in sorted(set(files))},
            "directories": sorted(directories)}


def data_mappings(root: Path, snapshot: dict) -> list[tuple[Path, str]]:
    result = []
    for name in snapshot["files"]:
        if name in RECIPE_FILES:
            continue
        path = Path(name)
        result.append((root / path, (Path("share/ai-batch-runner") / path.parent).as_posix()))
        # Package-relative assets formerly discovered by --collect-data must
        # use this enumeration too, or ignored generated files can leak in.
        if path.parts[0] == "pbatch" and path.suffix != ".py":
            result.append((root / path, path.parent.as_posix()))
    return result


def stage_inputs(root: Path, destination: Path, snapshot: dict) -> None:
    """Freeze the enumerated project inputs so the compiler reads one snapshot."""
    for name in snapshot["directories"]:
        (destination / name).mkdir(parents=True, exist_ok=True)
    for name in snapshot["files"]:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
    if source_snapshot(destination) != snapshot:
        raise RuntimeError("source inputs changed while staging the binary build")


def _git(root: Path, *args: str) -> bytes | None:
    try:
        process = subprocess.Popen(["git", "-C", str(root), *args],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        output, _ = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError("git metadata query timed out") from None
    except OSError:
        return None
    return output if process.returncode == 0 else None


def git_record(root: Path, snapshot: dict) -> dict:
    available_root = _is_git_root(root)
    commit = _git(root, "rev-parse", "HEAD") if available_root else None
    status = _git(root, "status", "--porcelain", "--untracked-files=no") if available_root else None
    tracked = _git(root, "ls-files", "-z", "--cached") if available_root else None
    available = commit is not None and status is not None and tracked is not None
    names = set(os.fsdecode(tracked).split("\0")) if tracked is not None else set()
    dirty = (bool(status) or not set(snapshot["files"]).issubset(names)) if available else None
    return {"commit": commit.decode("ascii").strip() if commit is not None else None,
            "dirty": dirty, "available": available,
            "dirty_scope": "tracked changes and untracked build inputs"}


def _is_git_root(root: Path) -> bool:
    directory = _git(root, "rev-parse", "--show-toplevel")
    return directory is not None and Path(os.fsdecode(directory).strip()).resolve() == root.resolve()


def environment_record() -> dict:
    try:
        pyinstaller = metadata.version("pyinstaller")
    except metadata.PackageNotFoundError:
        pyinstaller = None
    platform_name = "-".join((platform.system(), platform.release(), platform.machine()))
    return {"platform": platform_name, "system": platform.system(),
            "machine": platform.machine(), "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "pyinstaller": pyinstaller, "libc": list(platform.libc_ver())}


def dependency_records(workpath: Path, root: Path, snapshot: dict | None = None,
                       *, generated_root: Path | None = None) -> list[dict]:
    """Record actual external Analysis inputs after packaging, without claiming
    they were snapshotted before the build. Generated archives are not inputs.
    """
    snapshot = snapshot if snapshot is not None else source_snapshot(root)
    project_files = {root / name for name in snapshot["files"]}
    paths = set()
    for toc in workpath.rglob("Analysis-00.toc"):
        pending = [ast.literal_eval(toc.read_text(encoding="utf-8"))]
        while pending:
            value = pending.pop()
            if not isinstance(value, (list, tuple)):
                continue
            if len(value) == 3 and isinstance(value[1], str):
                candidate = Path(value[1])
                if candidate.is_absolute() and candidate.is_file():
                    paths.add(candidate.resolve())
            pending.extend(item for item in value if isinstance(item, (list, tuple)))
    return [{"path": str(path), **file_record(path)} for path in sorted(paths)
            if path not in project_files and not path.is_relative_to(generated_root or workpath)
            and not any(part in {".git", "__pycache__"} for part in path.parts)]


def sidecars(binary: Path) -> tuple[Path, Path]:
    return (binary.with_name(binary.name + ".manifest.json"),
            binary.with_name(binary.name + ".LICENSE"))


@contextmanager
def output_lock(binary: Path):
    """Nonblocking, stable per-artifact lock; never unlink its inode.

    Load the standard-library-only locking module without importing pbatch's
    runtime initialization. Cooperating builders hold this through publication.
    """
    locking = runpy.run_path(str(Path(__file__).resolve().parents[1] / "pbatch/file_lock.py"))
    path = binary.with_name("." + binary.name + ".build.lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        opened, current = os.fstat(descriptor), path.lstat()
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError("binary build lock must be a stable regular file")
        try:
            locking["flock"](descriptor, locking["LOCK_EX"] | locking["LOCK_NB"])
        except BlockingIOError:
            raise BuildBusyError(f"binary build is already in progress: {binary.name}") from None
        yield
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, raw: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_manifest(binary: Path, root: Path, snapshot: dict, git: dict,
                   workpath: Path, *, generated_root: Path | None = None) -> Path:
    manifest, license_file = sidecars(binary)
    license_bytes = (root / "LICENSE").read_bytes()
    license_record = {"sha256": hashlib.sha256(license_bytes).hexdigest(), "bytes": len(license_bytes)}
    if license_record != snapshot["files"]["LICENSE"]:
        raise RuntimeError("LICENSE changed during binary build")
    _atomic_write(license_file, license_bytes)
    data = {"schema_version": 1, "scope": SCOPE,
            "source": {**snapshot, "git": git},
            "binary": {"name": binary.name, **file_record(binary)},
            "license": {"name": license_file.name, **file_record(license_file)},
            "environment": environment_record(),
            "build_dependencies": dependency_records(workpath, root, snapshot,
                                                     generated_root=generated_root),
            "dependency_observation": "external Analysis files hashed after build; "
                                      "not a pre-build dependency snapshot"}
    unchanged_source(root, snapshot, git)
    _atomic_write(manifest, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return manifest


def unchanged_source(root: Path, snapshot: dict, git: dict) -> None:
    if source_snapshot(root) != snapshot or git_record(root, snapshot) != git:
        raise RuntimeError("source inputs changed during binary build; no manifest published")


def publish_candidate(candidate: Path, binary: Path, root: Path,
                      snapshot: dict, git: dict) -> None:
    """Publish only this build's private files, with success evidence last."""
    candidate_manifest, candidate_license = sidecars(candidate)
    manifest, license_file = sidecars(binary)
    data = json.loads(candidate_manifest.read_text(encoding="utf-8"))
    report = verify(candidate_manifest, root)
    if not report["ok"]:
        raise RuntimeError("candidate changed before publication")
    unchanged_source(root, snapshot, git)
    try:
        os.replace(candidate, binary)
        os.replace(candidate_license, license_file)
        for path, field in ((binary, "binary"), (license_file, "license")):
            if file_record(path) != {key: data[field][key] for key in ("sha256", "bytes")}:
                raise RuntimeError("candidate changed during publication")
        unchanged_source(root, snapshot, git)
        os.replace(candidate_manifest, manifest)
    except BaseException:
        manifest.unlink(missing_ok=True)
        raise


def _named_file(parent: Path, record: dict) -> Path:
    if not isinstance(record, dict):
        raise ValueError("manifest artifact record must be an object")
    name = record.get("name")
    if not isinstance(name, str) or name in {"", ".", ".."} or Path(name).name != name:
        raise ValueError("manifest artifact name must be a simple file name")
    return parent / name


def verify(manifest: Path, root: Path, binary: Path | None = None) -> dict:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("scope") != SCOPE:
        raise ValueError("unsupported binary manifest")
    failures = []
    for field in ("binary", "license"):
        expected = data[field]
        path = binary if field == "binary" and binary is not None else _named_file(manifest.parent, expected)
        if file_record(path) != {key: expected[key] for key in ("sha256", "bytes")}:
            failures.append(field + " content differs")
    source = data["source"]
    if {key: data["license"][key] for key in ("sha256", "bytes")} != source["files"]["LICENSE"]:
        failures.append("license differs from the recorded source license")
    observed = source_snapshot(root)
    if observed["directories"] != source["directories"]:
        failures.append("source directory inventory differs")
    if observed["files"] != source["files"]:
        failures.append("source file inventory or contents differ")
    recorded_commit = source["git"]["commit"]
    current_git = git_record(root, observed)
    if recorded_commit is not None:
        if current_git["commit"] != recorded_commit:
            failures.append("source git commit differs")
        elif current_git != source["git"]:
            failures.append("source git state differs")
    return {"schema_version": 1, "ok": not failures, "scope": SCOPE,
            "binary_sha256": data["binary"]["sha256"], "errors": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    check = commands.add_parser("verify", help="check artifact, license and current source inputs")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--source-root", type=Path, required=True)
    check.add_argument("--binary", type=Path)
    args = parser.parse_args(argv)
    try:
        report = verify(args.manifest, args.source_root.resolve(), args.binary)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        report = {"schema_version": 1, "ok": False, "scope": SCOPE, "errors": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
