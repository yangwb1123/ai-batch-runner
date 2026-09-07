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
WORKSPACE_DIFF_SCOPES = frozenset({
    "all", "unstaged", "staged", "untracked", "base", "commit",
})


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
    return workspace_diff_scoped(
        cwd, scope="all", path=path, max_bytes=max_bytes)


def workspace_diff_scoped(cwd: str, *, scope: str = "all", path: str = "",
                          target: str = "",
                          max_bytes: int = _DIFF_MAX_BYTES) -> dict[str, Any]:
    """Return one validated Git comparison scope without mutating the repo."""
    selected = str(scope or "all").strip().lower()
    if selected not in WORKSPACE_DIFF_SCOPES:
        return _diff_error(path, f"unsupported diff scope: {selected}", selected, target)
    status = capture_workspace(cwd)
    if not status.available:
        return _diff_error(path, status.error, selected, target)
    relative, error = _relative_path(status.root, cwd, path)
    if error:
        return _diff_error(path, error, selected, target)
    resolved, error = _resolve_diff_target(status.root, selected, target)
    if error:
        return _diff_error(relative, error, selected, target)
    tracked = _scoped_tracked_diff(
        status.root, selected, relative, resolved, max_bytes)
    if tracked[2]:
        return _diff_error(relative, tracked[2], selected, target)
    text, truncated = tracked[:2]
    untracked = _scope_untracked(status, selected)
    untracked_omitted = (
        selected in {"all", "untracked", "base"}
        and status.untracked_files > len(untracked))
    if relative:
        untracked = [item for item in untracked if item.path == relative]
    text, more_truncated = _append_untracked(
        status.root, text, untracked, max_bytes)
    files, files_truncated, file_error = _scope_files(
        status, selected, resolved)
    if file_error:
        return _diff_error(relative, file_error, selected, target)
    return {
        "path": relative,
        "scope": selected,
        "target": str(target or ""),
        "resolved_target": resolved,
        "patch": text or "No diff for the selected workspace scope.",
        "truncated": bool(truncated or more_truncated or untracked_omitted),
        "bytes": len(text.encode("utf-8", errors="replace")),
        "files": files,
        "files_truncated": files_truncated,
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


def _scoped_tracked_diff(root: str, scope: str, path: str, target: str,
                         cap: int) -> tuple[str, bool, str]:
    base = ["diff", "--no-ext-diff", "--no-color"]
    commands = _diff_commands(root, scope, target, base)
    return _run_diff_commands(root, commands, path, cap)


def _diff_commands(root: str, scope: str, target: str,
                   base: list[str]) -> list[list[str]]:
    if scope == "all":
        return ([base + ["HEAD"]] if _head_revision(root)
                else [base + ["--cached"], base])
    if scope == "staged":
        suffix = ["--cached"] + (["HEAD"] if _head_revision(root) else [])
        return [base + suffix]
    if scope == "unstaged":
        return [base]
    if scope == "base":
        return [base + [target]]
    if scope == "commit":
        return [["show", "--format=", "--no-ext-diff", "--no-color", target]]
    return []


def _run_diff_commands(root: str, commands: list[list[str]], path: str,
                       cap: int) -> tuple[str, bool, str]:
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


def _resolve_diff_target(root: str, scope: str,
                         target: str) -> tuple[str, str]:
    value = str(target or "").strip()
    if scope not in {"base", "commit"}:
        return ("", "diff target is only valid for base/commit scope") if value else ("", "")
    if not value:
        return "", f"{scope} diff scope requires a Git ref"
    if (value.startswith("-") or len(value.encode("utf-8")) > 200
            or any(ord(char) < 32 for char in value)):
        return "", "Git ref contains unsupported characters"
    result = _git(root, "rev-parse", "--verify", f"{value}^{{commit}}", cap=4096)
    if not result.ok:
        detail = _diagnostic(result.stderr, "unknown revision")
        return "", f"unable to resolve Git ref {value}: {detail}"
    return result.stdout.strip(), ""


def _scope_untracked(status: WorkspaceStatus,
                     scope: str) -> list[WorkspaceFile]:
    if scope not in {"all", "untracked", "base"}:
        return []
    return [item for item in status.files if item.untracked]


def _scope_files(status: WorkspaceStatus, scope: str,
                 target: str) -> tuple[list[dict[str, Any]], bool, str]:
    if scope in {"base", "commit"}:
        files, truncated, error = _target_scope_files(status.root, scope, target)
        if scope == "base" and not error:
            files = _merge_file_rows(
                files, [item.to_dict() for item in _scope_untracked(status, scope)])
        return files[:_STATUS_FILE_LIMIT], truncated or len(files) > _STATUS_FILE_LIMIT, error
    selected = [item for item in status.files if _file_matches_scope(item, scope)]
    expected = _scope_file_count(status, scope)
    return ([item.to_dict() for item in selected], expected > len(selected), "")


def _file_matches_scope(item: WorkspaceFile, scope: str) -> bool:
    if scope == "staged":
        return item.staged
    if scope == "unstaged":
        return item.unstaged
    if scope == "untracked":
        return item.untracked
    return True


def _scope_file_count(status: WorkspaceStatus, scope: str) -> int:
    if scope == "staged":
        return status.staged_files
    if scope == "unstaged":
        return status.unstaged_files
    if scope == "untracked":
        return status.untracked_files
    return status.total_files


def _target_scope_files(root: str, scope: str,
                        target: str) -> tuple[list[dict[str, Any]], bool, str]:
    args = (["diff", "--name-status", "-z", target, "--"]
            if scope == "base" else
            ["diff-tree", "--root", "--no-commit-id", "--name-status",
             "-r", "-z", target, "--"])
    result = _git(root, *args, cap=128 * 1024)
    exceeded = "output exceeds" in result.stderr
    if not result.ok and not exceeded:
        return [], False, _diagnostic(
            result.stderr, "unable to enumerate changed files")
    files = _parse_name_status(result.stdout)
    return files[:_STATUS_FILE_LIMIT], exceeded or len(files) > _STATUS_FILE_LIMIT, ""


def _parse_name_status(raw: str) -> list[dict[str, Any]]:
    fields, files, index = raw.split("\0"), [], 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        if index >= len(fields):
            break
        original, path = "", fields[index]
        index += 1
        if status[:1] in {"R", "C"} and index < len(fields):
            original, path = path, fields[index]
            index += 1
        if not _operational_path(path):
            files.append({"path": path, "status": status,
                          "original_path": original})
    return files


def _merge_file_rows(first: list[dict[str, Any]],
                     second: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged, seen = [], set()
    for row in first + second:
        path = str(row.get("path", ""))
        if path and path not in seen:
            merged.append(row)
            seen.add(path)
    return merged


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


def _diff_error(path: str, error: str, scope: str = "all",
                target: str = "") -> dict[str, Any]:
    return {"path": path, "scope": scope, "target": str(target or ""),
            "resolved_target": "", "patch": "", "truncated": False,
            "bytes": 0, "files": [], "files_truncated": False,
            "error": error}
