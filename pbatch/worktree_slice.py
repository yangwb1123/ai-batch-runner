"""Dirty-worktree seeding and write-scope checks for isolated agents.

The main worktree is treated as an immutable source snapshot.  This module
only reads the source worktree and materializes selected dirty paths into an
external Git worktree; it never resets, stashes, checks out, reverts, or
commits the source worktree.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


MAX_GIT_OUTPUT = 16 * 1024 * 1024
GIT_TIMEOUT = 30


@dataclass(frozen=True)
class DirtyRecord:
    status: str
    path: str


def normalize_patterns(patterns: Sequence[str]) -> tuple[str, ...]:
    """Validate workspace-relative allowlist patterns."""
    result = []
    for raw in patterns:
        value = str(raw or "").replace("\\", "/").strip()
        if not value or value in (".", "..") or value.startswith("/"):
            raise ValueError(f"workspace path must be relative: {raw!r}")
        parts = value.split("/")
        if ".." in parts or "" in parts:
            raise ValueError(f"workspace path is unsafe: {raw!r}")
        result.append(value.rstrip("/") or value)
    if not result:
        raise ValueError("at least one workspace path is required")
    return tuple(dict.fromkeys(result))


def path_matches(path: str, patterns: Sequence[str]) -> bool:
    """Match exact paths, directory prefixes, and bounded glob patterns."""
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if fnmatch.fnmatchcase(normalized, pattern):
            return True
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        if not _has_glob(pattern) and normalized.startswith(pattern + "/"):
            return True
    return False


def _has_glob(value: str) -> bool:
    return any(mark in value for mark in "*?[")


def _git(root: Path, args: Sequence[str], input_data: bytes | None = None,
         timeout: int = GIT_TIMEOUT):
    """Run a bounded Git query or return None when it is unverifiable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], input=input_data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if len(result.stdout) > MAX_GIT_OUTPUT or len(result.stderr) > MAX_GIT_OUTPUT:
        return None
    return result


def git_head(root: Path) -> str:
    """Return the immutable starting commit, or raise on an unknown base."""
    result = _git(root, ["rev-parse", "--verify", "HEAD"])
    if result is None or result.returncode != 0:
        raise ValueError("could not resolve Git HEAD")
    return result.stdout.decode("utf-8", errors="strict").strip()


def worktree_digest(root: Path) -> str:
    """Digest source state, excluding only pi-batch runtime files."""
    records = dirty_records(root)
    status = b"\0".join(
        f"{record.status} {record.path}".encode("utf-8", errors="surrogateescape")
        for record in records if not _runtime_path(record.path))
    diff = _git(root, ["diff", "--no-ext-diff", "--binary", "HEAD", "--"])
    if diff is None or diff.returncode != 0:
        raise ValueError("could not capture Git worktree state")
    untracked = _untracked_digest(root, records)
    return hashlib.sha256(status + b"\0" + diff.stdout + untracked).hexdigest()


def dirty_records(root: Path) -> tuple[DirtyRecord, ...]:
    """Read all non-ignored dirty paths with NUL-safe status parsing."""
    result = _git(root, ["status", "--porcelain=v1", "-z",
                         "--untracked-files=all"])
    if result is None or result.returncode != 0:
        raise ValueError("could not read Git worktree status")
    return _parse_status(result.stdout)


def _untracked_digest(root: Path, records: Sequence[DirtyRecord]) -> bytes:
    """Include bounded non-ignored untracked file contents in the baseline."""
    values = []
    total = 0
    for record in records:
        if not record.status.startswith("??") or _runtime_path(record.path):
            continue
        source = _safe_source(root, record.path)
        if not source.is_file():
            raise ValueError(f"untracked path is not a regular file: {record.path}")
        size = source.stat().st_size
        total += size
        if size > MAX_GIT_OUTPUT or total > MAX_GIT_OUTPUT:
            raise ValueError(f"untracked paths exceed snapshot limit: {record.path}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        values.append(f"{record.path}\0{digest}".encode("utf-8"))
    return b"\0".join(sorted(values))


def _runtime_path(path: str) -> bool:
    """Exclude only pi-batch runtime state from source-scope checks."""
    normalized = path.replace("\\", "/")
    return (normalized == ".pi-batch" or normalized.startswith(".pi-batch/")
            or normalized.startswith(".pi-batch.lock"))


def _parse_status(raw: bytes) -> tuple[DirtyRecord, ...]:
    fields = raw.split(b"\0")
    records: list[DirtyRecord] = []
    index = 0
    while index < len(fields) and fields[index]:
        entry = fields[index]
        if len(entry) >= 4:
            status = os.fsdecode(entry[:2])
            path = os.fsdecode(entry[3:])
            records.append(DirtyRecord(status, path))
            if "R" in status or "C" in status:
                index += 1
                if index < len(fields) and fields[index]:
                    records.append(DirtyRecord(status, os.fsdecode(fields[index])))
        index += 1
    return tuple(records)


def _selected_records(records: Iterable[DirtyRecord],
                      patterns: Sequence[str]) -> tuple[DirtyRecord, ...]:
    return tuple(record for record in records
                 if path_matches(record.path, patterns))


def _tracked_paths(root: Path, paths: Sequence[str]) -> tuple[str, ...]:
    if not paths:
        return ()
    result = _git(root, ["ls-files", "-z", "--", *paths])
    if result is None or result.returncode != 0:
        raise ValueError("could not resolve tracked dirty paths")
    values = [os.fsdecode(item) for item in result.stdout.split(b"\0") if item]
    return tuple(dict.fromkeys(values))


def _dirty_patch(root: Path, paths: Sequence[str]) -> bytes:
    if not paths:
        return b""
    result = _git(root, ["diff", "--binary", "HEAD", "--", *paths])
    if result is None or result.returncode != 0:
        raise ValueError("could not capture dirty Git patch")
    return result.stdout


def _apply_patch(worktree: Path, patch: bytes) -> None:
    if not patch:
        return
    result = _git(worktree, ["apply", "--binary", "--whitespace=nowarn", "-"], patch)
    if result is None or result.returncode != 0:
        detail = (result.stderr if result is not None else b"")
        raise ValueError("could not apply dirty slice: " +
                         detail.decode("utf-8", errors="replace")[:500])


def _safe_source(root: Path, relative: str) -> Path:
    source = root / relative
    try:
        resolved = source.resolve(strict=False)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ValueError(f"dirty path escapes workspace: {relative}") from exc
    if source.is_symlink():
        raise ValueError(f"symlink dirty path is not seedable: {relative}")
    return source


def _copy_untracked(root: Path, worktree: Path,
                    records: Sequence[DirtyRecord]) -> None:
    for record in records:
        if not record.status.startswith("??"):
            continue
        source = _safe_source(root, record.path)
        if not source.is_file():
            raise ValueError(f"untracked dirty path is not a regular file: {record.path}")
        target = worktree / record.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def seed_dirty_slice(root: Path, worktree: Path,
                     patterns: Sequence[str]) -> tuple[str, ...]:
    """Materialize selected current dirty paths into an external worktree."""
    normalized = normalize_patterns(patterns)
    selected = _selected_records(dirty_records(root), normalized)
    tracked = _tracked_paths(
        root, [record.path for record in selected
               if not record.status.startswith("??")])
    _apply_patch(worktree, _dirty_patch(root, tracked))
    _copy_untracked(root, worktree, selected)
    return tuple(sorted({record.path for record in selected}))


def changed_paths(worktree: Path) -> tuple[str, ...]:
    """Return source paths changed from the worktree HEAD."""
    return tuple(sorted({record.path for record in dirty_records(worktree)
                         if not _runtime_path(record.path)}))


def scope_snapshot(worktree: Path) -> dict[str, tuple[str, str]]:
    """Capture status/content for dirty paths before an agent starts."""
    snapshot = {}
    for record in dirty_records(worktree):
        if _runtime_path(record.path):
            continue
        snapshot[record.path] = (record.status, _file_signature(worktree, record.path))
    return snapshot


def _file_signature(root: Path, relative: str) -> str:
    """Bound the content evidence used to detect edits to seeded files."""
    path = root / relative
    if path.is_symlink():
        return "symlink:" + os.readlink(path)
    if not path.exists():
        return "missing"
    if not path.is_file():
        raise ValueError(f"scoped path is not a regular file: {relative}")
    size = path.stat().st_size
    if size > MAX_GIT_OUTPUT:
        raise ValueError(f"scoped path exceeds snapshot limit: {relative}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scope_violations(worktree: Path, patterns: Sequence[str],
                     baseline: dict[str, tuple[str, str]] | None = None) -> tuple[str, ...]:
    """Return newly changed paths outside an explicit write scope."""
    normalized = normalize_patterns(patterns)
    current = scope_snapshot(worktree)
    before = baseline or {}
    paths = set(current) | set(before)
    return tuple(sorted(path for path in paths
                        if current.get(path) != before.get(path)
                        and not path_matches(path, normalized)))


def patch_for_scope(worktree: Path, patterns: Sequence[str]) -> bytes:
    """Return a bounded tracked patch for human review/export."""
    normalized = normalize_patterns(patterns)
    paths = [path for path in changed_paths(worktree)
             if path_matches(path, normalized)]
    return _dirty_patch(worktree, paths)


def write_patch(worktree: Path, patterns: Sequence[str], target: Path) -> Path:
    """Write an external tracked patch and an untracked-path manifest."""
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(patch_for_scope(worktree, patterns))
    normalized = normalize_patterns(patterns)
    untracked = [record.path for record in dirty_records(worktree)
                 if record.status.startswith("??")
                 and path_matches(record.path, normalized)]
    if untracked:
        target.with_suffix(target.suffix + ".untracked").write_text(
            "\n".join(sorted(set(untracked))) + "\n", encoding="utf-8")
    return target
