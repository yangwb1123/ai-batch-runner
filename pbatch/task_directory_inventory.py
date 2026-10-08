"""Explicit bounded directory stat samples. No discovery, Git, links or body reads."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat

from .task_center_io import checked_path
from .task_directory_schema import MAX_ENTRIES, member_path
from .task_preservation_io import stat_fields
from .task_preservation_schema import MAX_PAYLOAD_BYTES


def _admit(info, device: int) -> None:
    if (not hasattr(os, "geteuid") or info.st_uid != os.geteuid() or info.st_dev != device or
            not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
        raise ValueError("directory copy requires owned same-device regular files/directories, no links or special objects")
    if info.st_mode & 0o7000 or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
        raise ValueError("directory copy refuses privileged modes or hard-link aliases")


@contextmanager
def _directory(path: Path, expected: dict, device: int):
    path = checked_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        _admit(before, device)
        if not stat.S_ISDIR(before.st_mode) or stat_fields(before) != expected:
            raise ValueError("directory changed before bounded enumeration")
        yield descriptor
        if stat_fields(os.fstat(descriptor)) != expected or stat_fields(checked_path(path).stat()) != expected:
            raise ValueError("directory changed during bounded enumeration")
    finally:
        os.close(descriptor)


def inventory(root: Path, expected: dict) -> list[dict]:
    """Sample every admitted name or refuse; never truncate or follow an alias."""
    root = checked_path(root)
    info = root.stat()
    _admit(info, expected["device"])
    if not stat.S_ISDIR(info.st_mode) or stat_fields(info) != expected:
        raise ValueError("directory root changed or is not a directory")
    rows = [{"path": ".", "stat": expected}]
    pending = [rows[0]]
    seen = {(info.st_dev, info.st_ino)}
    content_bytes = 0
    while pending:
        row = pending.pop()
        path = root if row["path"] == "." else root / row["path"]
        with _directory(path, row["stat"], expected["device"]) as descriptor:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if len(rows) >= MAX_ENTRIES:
                        raise ValueError("directory entry budget exhausted; no names were omitted")
                    relative = entry.name if row["path"] == "." else row["path"] + "/" + entry.name
                    member_path(relative)
                    current = entry.stat(follow_symlinks=False)
                    _admit(current, expected["device"])
                    identity = current.st_dev, current.st_ino
                    if identity in seen:
                        raise ValueError("directory copy repeats a filesystem identity")
                    seen.add(identity)
                    item = {"path": relative, "stat": stat_fields(current)}
                    rows.append(item)
                    if stat.S_ISDIR(current.st_mode):
                        pending.append(item)
                    else:
                        content_bytes += current.st_size
                        if content_bytes > MAX_PAYLOAD_BYTES:
                            raise ValueError("directory content byte budget exhausted; no truncation")
    return [rows[0], *sorted(rows[1:], key=lambda item: item["path"])]
