"""Linux fixture-only coordination, separate from SQLite record-lock descriptors.

Cooperating fixture writers and the harness serialize connections on their
owned scratch directory. Keep the original three-second contention budget;
noncooperating clients still rely on SQLite's remaining busy wait and refuse.
This does not change Agent, real-provider, task, acceptance or lease deadlines.
"""
from contextlib import closing, contextmanager
import fcntl
import math
import os
from pathlib import Path
import sqlite3
import stat
import time


def _directory(descriptor, parent):
    info, current = os.fstat(descriptor), parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or not stat.S_ISDIR(current.st_mode) or
            (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino) or
            info.st_uid != os.getuid() or current.st_uid != os.getuid()):
        raise sqlite3.OperationalError("provider evidence guard directory unavailable")


def _lock(descriptor, deadline):
    pause = 0.01
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError("provider evidence guard wait budget exhausted")
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise sqlite3.OperationalError("provider evidence cooperative store is locked") from None
            # Drift away from a repeated holder's poll phase without faster
            # spinning or extending the shared deadline; this is not FIFO.
            time.sleep(min(pause, remaining))
            pause = 0.011 if pause == 0.01 else 0.01


@contextmanager
def connection(path: Path, timeout: float):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("provider evidence guard timeout must be finite and positive")
    deadline = time.monotonic() + timeout
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _directory(descriptor, path.parent)
        _lock(descriptor, deadline)
        _directory(descriptor, path.parent)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise sqlite3.OperationalError("provider evidence guard wait budget exhausted")
        with closing(sqlite3.connect(path, timeout=remaining)) as value:
            yield value
    finally:
        os.close(descriptor)  # Killed participants release locks; no cleanup/breaking needed.
