"""POSIX cooperative pool-directory guard; never open a SQLite DB descriptor.

Private pool state already requires a real owned directory. This extends the
bootstrap's directory flock across pathname permission checks and transactions.
It coordinates participating PoolStore instances, not arbitrary SQLite clients,
malicious same-uid renames, filesystem I/O deadlines or account authorization.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import math
import os
from pathlib import Path
import stat
import time

from . import file_lock as fcntl


LOCK_TIMEOUT = 10.0  # Existing SQLite admission wait, shared rather than doubled.


def _check_directory(descriptor, parent):
    info, named = os.fstat(descriptor), parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or not stat.S_ISDIR(named.st_mode):
        raise OSError(errno.EINVAL, "pool guard requires a real directory")
    if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
        raise OSError(errno.EINVAL, "pool directory changed while acquiring guard")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or named.st_uid != os.getuid()):
        raise OSError(errno.EACCES, "pool guard directory must be owned by the current user")


def _acquire(descriptor, deadline):
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("pool cooperative state guard is busy") from None
            time.sleep(min(0.01, remaining))


@contextmanager
def state_guard(parent: Path):
    deadline = time.monotonic() + LOCK_TIMEOUT
    if not math.isfinite(LOCK_TIMEOUT) or LOCK_TIMEOUT <= 0:
        raise ValueError("pool guard timeout must be finite and positive")
    descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
    try:
        _check_directory(descriptor, parent)
        _acquire(descriptor, deadline)
        _check_directory(descriptor, parent)
        yield deadline
    finally:
        os.close(descriptor)  # No deletion/lock breaking; kernel releases on exit/kill.
