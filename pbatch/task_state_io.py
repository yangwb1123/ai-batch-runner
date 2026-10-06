"""Private bounded state writes; querying never repairs or creates state.

Locks are nonblocking: observational writers lose telemetry rather than hold
execution behind another writer. Explicit metadata operations report conflicts.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from . import file_lock
from .task_center_io import checked_path, read_object, _open_regular

MAX_STATE_BYTES = 128 * 1024
MAX_LEDGER_BYTES = 4 * 1024 * 1024
_APPEND_GUARDS = {}
_GUARD_LOCK = threading.Lock()
_FALLBACK_GUARD = threading.Lock()


def encode(value: dict) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise ValueError("task state exceeds its byte budget")
    return payload


@contextmanager
def locked(path: Path):
    path = checked_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    checked_path(path.parent)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.with_suffix(path.suffix + ".lock"), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("task state lock must be a regular file")
        file_lock.flock(descriptor, file_lock.LOCK_EX | file_lock.LOCK_NB)
        yield path
    finally:
        os.close(descriptor)


def atomic_write(path: Path, value: dict) -> None:
    payload = encode(value)
    path = checked_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".task-state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # file flush + atomic replace; directory fsync is POSIX-only
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def append_bounded(path: Path, value: dict) -> None:
    incoming = encode(value) + b"\n"
    path = checked_path(path)
    with _GUARD_LOCK:
        if path not in _APPEND_GUARDS and len(_APPEND_GUARDS) < 128:
            _APPEND_GUARDS[path] = threading.Lock()
        guard = _APPEND_GUARDS.get(path, _FALLBACK_GUARD)
    if not guard.acquire(timeout=1.0):
        raise BlockingIOError("local task observation writer is busy")
    try:
        _append_line(path, incoming)
    finally:
        guard.release()


def _append_line(path: Path, incoming: bytes) -> None:
    with locked(path):
        try:
            with _open_regular(path) as handle:
                size = os.fstat(handle.fileno()).st_size
                if size + len(incoming) > MAX_LEDGER_BYTES:
                    handle.seek(max(0, size - MAX_LEDGER_BYTES // 2))
                    tail = handle.read(MAX_LEDGER_BYTES // 2)
                else:
                    tail = None
        except FileNotFoundError:
            tail = None
        if tail is not None:
            _replace_ledger(path, tail.partition(b"\n")[2])
        flags = (os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("task ledger must be a regular file")
            if os.write(descriptor, incoming) != len(incoming):
                raise OSError("short task ledger append")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _replace_ledger(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".task-ledger-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path: Path) -> dict | None:
    return read_object(path, MAX_STATE_BYTES)
