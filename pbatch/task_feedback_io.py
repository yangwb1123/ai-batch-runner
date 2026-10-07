"""Bounded private feedback I/O. Exclusive copies are evidence, never capacity-cleaned."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from .task_center_io import checked_path, _open_regular
from .task_preservation_io import remove_owned_temporary
from .task_state_io import _sync_directory


def stat_key(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def private_root(root: Path) -> None:
    info = checked_path(root).stat()
    if (not stat.S_ISDIR(info.st_mode) or not hasattr(os, "geteuid") or
            info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError("feedback storage must be an owned private directory")


def read_bytes(path: Path, maximum: int, *, private: bool = False) -> tuple[bytes, tuple]:
    with _open_regular(path) as handle:
        before = os.fstat(handle.fileno())
        if not hasattr(os, "geteuid") or before.st_uid != os.geteuid() or before.st_nlink != 1:
            raise ValueError("feedback input must be an owned unaliased regular file; OS uid is not operator authentication")
        if private and before.st_mode & 0o077:
            raise ValueError("feedback metadata/copy is not private")
        if before.st_size > maximum:
            raise ValueError("feedback input byte budget exceeded; no truncation")
        raw = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
    current = checked_path(path).stat()
    if len(raw) > maximum or stat_key(before) != stat_key(after) or stat_key(after) != stat_key(current) or len(raw) != after.st_size:
        raise ValueError("feedback input changed during bounded read or exceeded byte budget")
    return raw, stat_key(current)


def proof(path: Path, raw: bytes) -> dict:
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def publish(path: Path, raw: bytes) -> None:
    """Publish exclusively and fsync. Cleanup only our unpublished temporary inode."""
    descriptor, name = tempfile.mkstemp(prefix=".feedback-", dir=path.parent)
    temporary, created = Path(name), os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # EEXIST preserves old copies and orphan evidence
        _sync_directory(path.parent)
    finally:
        remove_owned_temporary(temporary, created)
