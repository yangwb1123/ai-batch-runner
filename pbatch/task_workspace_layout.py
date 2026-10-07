"""Bounded, symlink-rejecting Git metadata scope admission, never ownership grants."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from .task_center_io import _open_regular, checked_path

MAX_ENTRIES = 2048
MAX_CONFIG_BYTES = 64 * 1024
_SECTION = re.compile(r'\[\s*([A-Za-z][A-Za-z0-9.-]*)(?:\s+"[^"\\]*")?\s*\]\s*(?:[#;].*)?\Z')


def stat_key(path: Path) -> tuple:
    value = checked_path(path).stat()
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _owned(path: Path, directory: bool) -> tuple:
    value = stat_key(path)
    if not hasattr(os, "geteuid") or value[3] != os.geteuid():
        raise ValueError("workspace or Git metadata OS uid is not the current effective user")
    if not (stat.S_ISDIR(value[2]) if directory else stat.S_ISREG(value[2])):
        raise ValueError("workspace metadata has an unsupported file type")
    return value


def read_metadata(path: Path, maximum: int) -> str:
    before = _owned(path, False)
    with _open_regular(path) as handle:
        raw = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > maximum:
        raise ValueError("Git metadata input byte budget exceeded")
    if before != stat_key(path) or (before[0], before[1]) != (after.st_dev, after.st_ino):
        raise ValueError("Git metadata changed during bounded read")
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Git metadata is not strict UTF-8") from exc


def _pointer(path: Path, prefix: str = "") -> Path:
    text = read_metadata(path, 4096)
    if not text.endswith("\n") or text.count("\n") != 1 or not text.startswith(prefix):
        raise ValueError("unsupported Git metadata pointer")
    value = text[len(prefix):-1]
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError("unsupported Git metadata pointer")
    target = Path(value)
    return checked_path(target if target.is_absolute() else path.parent / target)


def admit_layout(workspace: str, common_dir: str = "") -> dict:
    root = checked_path(workspace)
    _owned(root, True)
    marker = root / ".git"
    if marker.is_dir() and not marker.is_symlink():
        git_dir = checked_path(marker)
        common = checked_path(common_dir) if common_dir else git_dir
        if common != git_dir:
            raise ValueError("declared common directory does not match the registered repository")
        kind = "standalone"
    else:
        if not common_dir:
            raise ValueError("linked worktree needs explicit --common-dir metadata-read authorization")
        common = checked_path(common_dir)
        git_dir = _pointer(marker, "gitdir: ")
        if git_dir.parent != common / "worktrees" or not git_dir.name:
            raise ValueError("Git pointer is outside the explicitly selected linked-worktree metadata scope")
        if _pointer(git_dir / "commondir") != common or _pointer(git_dir / "gitdir") != marker:
            raise ValueError("linked-worktree metadata does not point back to the registered workspace")
        kind = "linked_worktree"
    _owned(common, True)
    _owned(git_dir, True)
    return {"workspace": root, "git_dir": git_dir, "common_dir": common, "layout": kind,
            "workspace_identity": _owned(root, True)[:4], "git_marker_identity": stat_key(marker)}


def _config(path: Path) -> None:
    if not path.exists():
        return
    text = read_metadata(path, MAX_CONFIG_BYTES)
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if any(ord(char) < 32 and char != "\t" for char in line) or "\\" in line:
            raise ValueError("unsupported Git configuration escapes or multiline values")
        if line.startswith("["):
            match = _SECTION.fullmatch(line)
            if not match:
                raise ValueError("unsupported Git configuration section syntax")
            section = match[1].lower()
            if section.split(".")[0] in {"include", "includeif", "filter"}:
                raise ValueError("Git configuration includes or executable filters are outside inspection scope")
        else:
            _config_key(section, line)


def _config_key(section: str, line: str) -> None:
    match = re.match(r"([A-Za-z][A-Za-z0-9-]*)(?:\s*=|\s*(?:[#;]|$))", line)
    if not section or not match:
        raise ValueError("unsupported Git configuration key syntax")
    key = match[1].lower()
    if section == "core" and (key == "worktree" or (key == "bare" and not re.fullmatch(
            r"bare\s*=\s*(?:false|no|off|0)\s*(?:[#;].*)?", line, re.I))):
        raise ValueError("bare or redirected Git workspaces are outside inspection scope")
    if key in {"partialclone", "promisor"}:
        raise ValueError("partial-clone fetching is outside read-only inspection scope")


def metadata_stamp(layout: dict) -> str:
    """Bound stat enumeration only; no recursive source-file scan or content equality claim."""
    common, git_dir = layout["common_dir"], layout["git_dir"]
    _config(common / "config")
    _config(git_dir / "config.worktree")
    records = [("workspace_identity", _owned(layout["workspace"], True)[:4]),
               ("git_marker", stat_key(layout["workspace"] / ".git"))]
    for current, directories, files in os.walk(common, followlinks=False, onerror=_walk_error):
        for name in [".", *sorted(directories), *sorted(files)]:
            path = Path(current) if name == "." else Path(current) / name
            if len(records) >= MAX_ENTRIES:
                raise ValueError("Git metadata entry budget exhausted; inspection did not assume cleanliness")
            if name.endswith(".lock"):
                raise ValueError("Git metadata lock exists; inspection did not remove or break it")
            info = stat_key(path)
            if not (stat.S_ISREG(info[2]) or stat.S_ISDIR(info[2])):
                raise ValueError("Git metadata contains a nonregular entry")
            if not hasattr(os, "geteuid") or info[3] != os.geteuid():
                raise ValueError("Git metadata entry OS uid is not the current effective user")
            if name in {"alternates", "http-alternates"} and info[4] != 0:
                raise ValueError("external Git object stores are outside inspection scope")
            records.append((str(path.relative_to(common)), info))
    _owned(layout["workspace"], True)
    return hashlib.sha256(repr(records).encode("utf-8")).hexdigest()


def _walk_error(error) -> None:
    raise error  # inaccessible metadata is not an empty/clean subtree


def operation_markers(layout: dict) -> list[str]:
    names = ("index.lock", "HEAD.lock", "config.lock", "packed-refs.lock", "MERGE_HEAD", "CHERRY_PICK_HEAD",
             "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply", "sequencer", "locked")
    return [name for name in names if any(os.path.lexists(root / name)
            for root in {layout["git_dir"], layout["common_dir"]})]
