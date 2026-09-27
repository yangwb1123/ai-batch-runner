"""POSIX directory-anchored, private transactions for user login credentials."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import secrets
import stat
import time

from .hub_client import HubError

STATE_FILE = "session.json"
LOCK_FILE = "session.lock"
MAX_STATE_BYTES = 32 * 1024
ERROR_MESSAGE = "private user credentials are invalid or unavailable"


def unavailable() -> HubError:
    return HubError(ERROR_MESSAGE, code="user_credentials_unavailable", status=401)


def _flags() -> int:
    required = ("O_NOFOLLOW", "O_NONBLOCK", "O_DIRECTORY", "getuid")
    if not all(hasattr(os, name) for name in required):
        raise unavailable()
    return os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _open_directory(path: str, *, create: bool) -> int | None:
    """Walk from / with no symlink component; create only private missing dirs."""
    flags = _flags() | os.O_RDONLY | os.O_DIRECTORY
    descriptor = os.open("/", flags)
    try:
        for component in Path(path).parts[1:]:
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | _flags(),
                                dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                os.fsync(descriptor)
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | _flags(),
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise unavailable()
        result, descriptor = descriptor, -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _private_file(descriptor: int, *, lock: bool = False):
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid() or info.st_nlink != 1):
        raise unavailable()
    if lock and info.st_size != 0:
        raise unavailable()
    if not lock and not 2 <= info.st_size <= MAX_STATE_BYTES:
        raise unavailable()
    return info


def _identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _entry_identity(directory: int, name: str):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _identity(info)


def _acquire(descriptor: int, timeout: float) -> None:
    from . import file_lock as fcntl

    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in {errno.EAGAIN, errno.EACCES}:
                raise
            if time.monotonic() >= deadline:
                raise unavailable() from None
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))


class PrivateTransaction:
    def __init__(self, path: str, directory: int, lock: int):
        self.path, self.directory, self.lock = path, directory, lock
        self._state_identity = None

    def check(self) -> None:
        current = _open_directory(self.path, create=False)
        if current is None:
            raise unavailable()
        try:
            if _identity(os.fstat(current)) != _identity(os.fstat(self.directory)):
                raise unavailable()
        finally:
            os.close(current)
        info = _private_file(self.lock, lock=True)
        if _entry_identity(self.directory, LOCK_FILE) != _identity(info):
            raise unavailable()

    def read(self) -> bytes | None:
        self.check()
        try:
            descriptor = os.open(STATE_FILE, os.O_RDONLY | _flags(), dir_fd=self.directory)
        except FileNotFoundError:
            self._state_identity = None
            return None
        try:
            info = _private_file(descriptor)
            chunks, size = [], 0
            while size <= MAX_STATE_BYTES:
                chunk = os.read(descriptor, min(4096, MAX_STATE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size != info.st_size or size > MAX_STATE_BYTES:
                raise unavailable()
            self._state_identity = _identity(info)
            self._check_state()
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _check_state(self) -> None:
        self.check()
        if _entry_identity(self.directory, STATE_FILE) != self._state_identity:
            raise unavailable()

    def write(self, raw: bytes) -> None:
        if not 2 <= len(raw) <= MAX_STATE_BYTES:
            raise unavailable()
        name = ".session-" + secrets.token_hex(16) + ".tmp"
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _flags(),
                             0o600, dir_fd=self.directory)
        try:
            _write_all(descriptor, raw)
            _private_file(descriptor)
            os.fsync(descriptor)
            self._check_state()
            os.replace(name, STATE_FILE, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            self._state_identity = _identity(os.fstat(descriptor))
            os.fsync(self.directory)
            self._check_state()
        finally:
            os.close(descriptor)
            try:
                os.unlink(name, dir_fd=self.directory)
            except FileNotFoundError:
                pass


def _write_all(descriptor: int, raw: bytes) -> None:
    remaining = memoryview(raw)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise unavailable()
        remaining = remaining[written:]


@contextmanager
def transaction(path: str, *, create: bool, timeout: float):
    directory, lock = None, None
    try:
        directory = _open_directory(path, create=create)
        if directory is None:
            yield None
            return
        lock = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | _flags(), 0o600,
                       dir_fd=directory)
        _private_file(lock, lock=True)
        _acquire(lock, timeout)
        item = PrivateTransaction(path, directory, lock)
        item.check()
        yield item
        item.check()
    except (OSError, ValueError, TypeError, ImportError):
        raise unavailable() from None
    finally:
        if lock is not None:
            os.close(lock)
        if directory is not None:
            os.close(directory)
