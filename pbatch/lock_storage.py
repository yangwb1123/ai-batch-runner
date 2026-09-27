"""Bounded lock metadata and serialized publication/reclamation.

The permanent guard inode must never be removed during normal operation.
Its OS lock protects the metadata check and mutation as one transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import itertools
import os
import stat
import tempfile
import time
from pathlib import Path

from . import file_lock
from .process_identity import valid_pid

MAX_HOLDER_BYTES = 65536
GUARD_TIMEOUT = 5.0
MAX_HOLDERS = 128


class OwnedLockPath(type(Path())):
    """A Path returned to the caller, retaining this acquisition's generation."""


def _open_regular(path: Path, flags: int) -> int:
    if path.is_symlink():
        raise OSError("lock path must not be a symlink")
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("lock path must be a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def lock_guard(path: Path, *, deadline: float | None = None):
    """Serialize short transactions, with a finite wait and no inode deletion."""
    guard = path.with_name(path.name + ".guard")
    deadline = time.monotonic() + GUARD_TIMEOUT if deadline is None else deadline
    descriptor = _open_regular(guard, os.O_RDWR | os.O_CREAT)
    try:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("instance lock coordination timed out")
            try:
                file_lock.flock(descriptor, file_lock.LOCK_EX | file_lock.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("instance lock coordination timed out")
                time.sleep(min(0.05, remaining))
        if not os.path.samestat(os.fstat(descriptor), guard.stat(follow_symlinks=False)):
            raise OSError("instance lock guard was replaced")
        yield
    finally:
        os.close(descriptor)


def _valid_fields(value: dict) -> bool:
    start = value.get("start", 0)
    if type(start) not in (int, float) or not 0 <= start <= 1e15:
        return False
    if any(key in value and type(value[key]) is not bool
           for key in ("worktree_dirty", "outputs_complete")):
        return False
    texts = [value.get(key, "") for key in
             ("host", "argv", "token", "workspace", "domain", "session_name")]
    outputs = value.get("outputs", [])
    return (valid_pid(value.get("pid")) and isinstance(outputs, list)
            and all(_safe_text(item) for item in texts + outputs))


def _safe_text(value) -> bool:
    if not isinstance(value, str) or "\0" in value:
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


def read_holder(path: Path) -> dict:
    """Unreadable, oversized or malformed metadata stays unavailable."""
    try:
        descriptor = _open_regular(path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_HOLDER_BYTES + 1)
        if len(raw) > MAX_HOLDER_BYTES:
            return {}
        value = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) and _valid_fields(value) else {}


def publish_holder(path: Path, payload: dict, *, staging_dir: Path | None = None) -> bool:
    """Publish complete metadata if absent; caller must hold the guard."""
    raw = json.dumps(payload, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_HOLDER_BYTES:
        raise OSError("instance lock metadata exceeds its size limit")
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=staging_dir or path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(temporary)


def holder_paths(path: Path) -> list[Path]:
    """Enumerate every participant under the coordinator lock, with a bound."""
    values = [path] if path.exists() or path.is_symlink() else []
    directory = path.with_name(path.name + ".holders")
    if directory.is_symlink():
        raise OSError("instance holder directory must not be a symlink")
    if directory.exists():
        values.extend(itertools.islice(directory.iterdir(), MAX_HOLDERS + 1))
    if len(values) > MAX_HOLDERS:
        raise OSError("instance holder count exceeds its limit")
    return values


def participant_path(path: Path, token: str) -> Path:
    directory = path.with_name(path.name + ".holders")
    if directory.is_symlink():
        raise OSError("instance holder directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory / (token + ".json")
