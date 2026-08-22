"""Bounded, read-only Git workspace inspection for the terminal UI."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runner import run_argv


_STATUS_FILE_LIMIT = 200
_DIFF_MAX_BYTES = 256 * 1024
_GIT_TIMEOUT = 10


@dataclass(frozen=True)
class WorkspaceFile:
    """One porcelain-v1 workspace entry."""

    path: str
    status: str
    original_path: str = ""

    @property
    def staged(self) -> bool:
        return self.status[0] not in {" ", "?"}

    @property
    def unstaged(self) -> bool:
        return self.status[1] not in {" ", "?"}

    @property
    def untracked(self) -> bool:
        return self.status == "??"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "original_path": self.original_path,
            "staged": self.staged,
            "unstaged": self.unstaged,
            "untracked": self.untracked,
        }


@dataclass(frozen=True)
class WorkspaceStatus:
    """Bounded snapshot suitable for the status panel and slash commands."""

    available: bool
    root: str = ""
    branch: str = ""
    head: str = ""
    files: tuple[WorkspaceFile, ...] = ()
    total_files: int = 0
    staged_files: int = 0
    unstaged_files: int = 0
    untracked_files: int = 0
    error: str = ""
    fingerprint: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "root": self.root,
            "branch": self.branch,
            "head": self.head[:12],
            "clean": self.available and self.total_files == 0,
            "changed": self.total_files,
            "staged": self.staged_files,
            "unstaged": self.unstaged_files,
            "untracked": self.untracked_files,
            "truncated": self.total_files > len(self.files),
            "error": self.error,
        }

    def details(self) -> dict[str, Any]:
        return {**self.summary(), "files": [item.to_dict() for item in self.files]}


def capture_workspace(cwd: str, limit: int = _STATUS_FILE_LIMIT) -> WorkspaceStatus:
    """Capture Git status without mutating the index or working tree."""
    root_result = _git(cwd, "rev-parse", "--show-toplevel")
    if not root_result.ok:
        return WorkspaceStatus(False, error=_diagnostic(
            root_result.stderr, "not a Git repository"))
    root = str(Path(root_result.stdout.strip()).resolve())
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if not status.ok:
        return WorkspaceStatus(False, root=root, error=_diagnostic(
            status.stderr, "unable to inspect Git status"))
    parsed = _parse_porcelain(status.stdout)
    branch = _branch_name(root)
    head = _head_revision(root)
    bounded = tuple(parsed[:max(1, int(limit))])
    fingerprint = _workspace_fingerprint(root, head, parsed)
    return WorkspaceStatus(
        True, root=root, branch=branch, head=head, files=bounded,
        total_files=len(parsed),
        staged_files=sum(item.staged for item in parsed),
        unstaged_files=sum(item.unstaged for item in parsed),
        untracked_files=sum(item.untracked for item in parsed),
        fingerprint=fingerprint,
    )


def workspace_delta(before: WorkspaceStatus,
                    after: WorkspaceStatus) -> dict[str, Any]:
    """Describe observable workspace change between two bounded snapshots."""
    if not before.available or not after.available:
        return {"changed": False, "paths": [], "head_changed": False}
    old = {item.path: item.status for item in before.files}
    new = {item.path: item.status for item in after.files}
    paths = sorted(path for path in old.keys() | new.keys()
                   if old.get(path) != new.get(path))
    head_changed = before.head != after.head
    fingerprint_changed = before.fingerprint != after.fingerprint
    if fingerprint_changed and not paths:
        paths = sorted(new)[:20]
    return {
        "changed": bool(head_changed or fingerprint_changed),
        "paths": paths[:20],
        "paths_truncated": len(paths) > 20,
        "head_changed": head_changed,
        "dirty_files": after.total_files,
    }


def workspace_diff(cwd: str, path: str = "",
                   max_bytes: int = _DIFF_MAX_BYTES) -> dict[str, Any]:
    """Return a bounded unified diff, including untracked files."""
    status = capture_workspace(cwd)
    if not status.available:
        return _diff_error(path, status.error)
    relative, error = _relative_path(status.root, cwd, path)
    if error:
        return _diff_error(path, error)
    tracked = _tracked_diff(status.root, relative, max_bytes)
    if tracked[2]:
        return _diff_error(relative, tracked[2])
    text, truncated = tracked[:2]
    untracked = [item for item in status.files if item.untracked]
    if relative:
        untracked = [item for item in untracked if item.path == relative]
    text, more_truncated = _append_untracked(
        status.root, text, untracked, max_bytes)
    return {
        "path": relative,
        "patch": text or "No diff for the selected workspace scope.",
        "truncated": bool(truncated or more_truncated),
        "bytes": len(text.encode("utf-8", errors="replace")),
        "error": "",
    }


def _git(cwd: str, *args: str, cap: int = _DIFF_MAX_BYTES):
    return run_argv(["git", "-C", cwd, *args], cwd, timeout=_GIT_TIMEOUT, cap=cap)


def _diagnostic(value: str, fallback: str) -> str:
    text = " ".join(str(value or "").split())
    return text[:500] or fallback


def _operational_path(path: str) -> bool:
    return path == ".pi-batch" or path.startswith(".pi-batch/")


def _parse_porcelain(raw: str) -> list[WorkspaceFile]:
    fields, entries, index = raw.split("\0"), [], 0
    while index < len(fields) and fields[index]:
        field = fields[index]
        if len(field) < 4:
            index += 1
            continue
        status, path, original = field[:2], field[3:], ""
        if ("R" in status or "C" in status) and index + 1 < len(fields):
            index += 1
            original = fields[index]
        if not _operational_path(path):
            entries.append(WorkspaceFile(path, status, original))
        index += 1
    return entries


def _branch_name(root: str) -> str:
    result = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if result.ok and result.stdout.strip():
        return result.stdout.strip()[:200]
    return "detached" if _head_revision(root) else "unborn"


def _head_revision(root: str) -> str:
    result = _git(root, "rev-parse", "--verify", "HEAD")
    return result.stdout.strip() if result.ok else ""


def _workspace_fingerprint(root: str, head: str,
                           files: list[WorkspaceFile]) -> str:
    digest = hashlib.sha256(head.encode("utf-8"))
    for item in files:
        digest.update(f"\0{item.status}\0{item.path}".encode("utf-8", errors="replace"))
        try:
            stat = os.lstat(Path(root) / item.path)
            digest.update(f"\0{stat.st_size}\0{stat.st_mtime_ns}".encode("ascii"))
        except OSError:
            digest.update(b"\0missing")
    return digest.hexdigest()


def _relative_path(root: str, cwd: str, value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    candidate = Path(os.path.abspath(candidate))
    try:
        return candidate.relative_to(Path(root)).as_posix(), ""
    except ValueError:
        return "", "diff path must stay inside the Git workspace"


def _tracked_diff(root: str, path: str,
                  cap: int) -> tuple[str, bool, str]:
    base = ["diff", "--no-ext-diff", "--no-color"]
    commands = ([base + ["HEAD"]] if _head_revision(root)
                else [base + ["--cached"], base])
    text, overflow = "", False
    for command in commands:
        remaining = cap - len(text.encode("utf-8", errors="replace"))
        if remaining <= 0:
            return text, True, ""
        args = command + (["--", path] if path else [])
        result = _git(root, *args, cap=remaining)
        exceeded = "output exceeds" in result.stderr
        if not result.ok and not exceeded:
            return "", False, _diagnostic(
                result.stderr, "unable to generate Git diff")
        text += result.stdout
        overflow = overflow or exceeded
    return text, overflow, ""


def _append_untracked(root: str, initial: str, files: list[WorkspaceFile],
                      cap: int) -> tuple[str, bool]:
    text, truncated = initial, False
    for item in files:
        remaining = cap - len(text.encode("utf-8", errors="replace"))
        if remaining <= 0:
            return text, True
        target = str(Path(root) / item.path)
        result = _git(root, "diff", "--no-index", "--no-color", "--",
                      os.devnull, target, cap=remaining)
        if result.exit_code not in {0, 1} and "output exceeds" not in result.stderr:
            continue
        text += result.stdout
        truncated = truncated or "output exceeds" in result.stderr
    return text, truncated


def _diff_error(path: str, error: str) -> dict[str, Any]:
    return {"path": path, "patch": "", "truncated": False,
            "bytes": 0, "error": error}
