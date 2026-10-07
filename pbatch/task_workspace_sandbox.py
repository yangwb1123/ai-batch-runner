"""Private temporary Git control metadata; repository config is never executable policy.

Only HEAD/index are copied. Explicitly admitted objects/refs remain read-only
references; this is not an OS sandbox or an atomic filesystem snapshot.
"""
from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .task_center_io import _open_regular, checked_path
from .task_workspace_layout import MAX_CONFIG_BYTES, read_metadata, stat_key

MAX_INDEX_BYTES = 1024 * 1024


def _copy_index(source: Path, target: Path) -> None:
    if not source.exists():
        return
    before = stat_key(source)
    with _open_regular(source) as handle:
        raw = handle.read(MAX_INDEX_BYTES + 1)
        info = os.fstat(handle.fileno())
    if len(raw) > MAX_INDEX_BYTES:
        raise ValueError("Git index copy budget exceeded; no records were truncated")
    if before != stat_key(source) or (before[0], before[1]) != (info.st_dev, info.st_ino):
        raise ValueError("Git index changed during bounded private copy")
    target.write_bytes(raw)
    target.chmod(0o600)


def _configuration(layout: dict) -> str:
    source = layout["common_dir"] / "config"
    text = read_metadata(source, MAX_CONFIG_BYTES) if source.exists() else ""
    section, object_format = "", "sha1"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line[1:].split("]", 1)[0].strip().lower()
        elif section == "extensions" and re.match(r"objectformat\s*=", line, re.I):
            object_format = line.split("=", 1)[1].strip().lower()
        elif section == "extensions" and re.match(r"refstorage\s*=", line, re.I):
            raise ValueError("non-files Git ref storage requires separate inspection support")
    if object_format not in {"sha1", "sha256"}:
        raise ValueError("unsupported repository object format")
    version = "1" if object_format == "sha256" else "0"
    return ("[core]\nrepositoryformatversion = " + version + "\nbare = false\nfilemode = true\n" +
            ("[extensions]\nobjectformat = sha256\n" if object_format == "sha256" else ""))


@contextmanager
def frozen_controls(layout: dict):
    """Own-only ephemeral files; no common-dir pointer or repository config includes."""
    config = _configuration(layout)
    head = read_metadata(layout["git_dir"] / "HEAD", 4096)
    temporary_root = checked_path("/tmp" if os.name == "posix" else tempfile.gettempdir())
    if any(temporary_root == root or root in temporary_root.parents
           for root in (layout["workspace"], layout["common_dir"])):
        raise ValueError("private temporary metadata must stay outside the inspected workspace and Git scope")
    with tempfile.TemporaryDirectory(prefix="pbatch-workspace-inspect-", dir=temporary_root) as name:
        private = checked_path(name)
        private.chmod(0o700)
        controls = private / "git"
        controls.mkdir(mode=0o700)
        for file, text in (("HEAD", head), ("config", config)):
            (controls / file).write_text(text, encoding="utf-8")
            (controls / file).chmod(0o600)
        _copy_index(layout["git_dir"] / "index", controls / "index")
        for file in ("objects", "refs", "packed-refs"):
            target = checked_path(layout["common_dir"] / file)
            if target.exists():
                (controls / file).symlink_to(target, target_is_directory=target.is_dir())
        if not (controls / "refs").exists():
            (controls / "refs").mkdir(mode=0o700)
        yield {**layout, "git_dir": controls}
