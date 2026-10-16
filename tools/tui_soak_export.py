"""Fail-closed, single-harness JSONL export; no partial-file recovery or replay."""
import errno
import os
from pathlib import Path
import stat


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _private(info, failure):
    if stat.S_ISDIR(info.st_mode):
        # Retain the original export ABI's OSError for a directory obstruction.
        raise IsADirectoryError(errno.EISDIR, "provider evidence export is a directory; preserve it")
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise failure("provider evidence export must be an owned unaliased regular file")


def _parent(path, failure):
    named = path.lstat()
    if not stat.S_ISDIR(named.st_mode) or named.st_uid != os.getuid():
        raise failure("provider evidence export directory changed or is unavailable")
    return named.st_dev, named.st_ino, named.st_mode, named.st_uid


def _directory(descriptor, path, failure):
    opened = os.fstat(descriptor)
    identity = opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid
    if not stat.S_ISDIR(opened.st_mode) or identity != _parent(path, failure):
        raise failure("provider evidence export directory changed or is unavailable")
    return identity


class Export:
    def __init__(self, path: Path, failure):
        self.path, self.failure = path, failure
        self.identity, self.failed = None, False
        self.parent = _parent(path.parent, failure)
        self.require_usable()

    def require_usable(self):
        if self.failed:
            raise self.failure("provider evidence export refused; preserve it without another append")
        try:
            self._current()
        except BaseException:
            self.failed = True
            raise

    def _current(self):
        if _parent(self.path.parent, self.failure) != self.parent:
            raise self.failure("provider evidence export directory changed since reader creation")
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            if self.identity is not None:
                raise self.failure("provider evidence export disappeared; preserve remaining evidence")
            return
        _private(current, self.failure)
        if self.identity is None or _stamp(current) != self.identity:
            raise self.failure("provider evidence export already exists or changed; no adoption or repair")

    def append(self, raw: bytes):
        self.require_usable()
        try:
            self._append(raw)
        except BaseException:
            self.failed = True
            raise

    def _open(self, directory):
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        if self.identity is None:
            flags |= os.O_CREAT | os.O_EXCL
        else:
            named = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            _private(named, self.failure)
            if _stamp(named) != self.identity:
                raise self.failure("provider evidence export changed before append")
        descriptor = os.open(self.path.name, flags, 0o600, dir_fd=directory)
        try:
            return os.fdopen(descriptor, "ab", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise

    def _admit(self, output, directory):
        opened = os.fstat(output.fileno())
        named = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
        _private(opened, self.failure)
        _private(named, self.failure)
        if (_stamp(opened) != _stamp(named)
                or (self.identity is not None and _stamp(opened) != self.identity)
                or (self.identity is None and opened.st_size != 0)):
            raise self.failure("provider evidence export changed while opening")
        return opened.st_size

    def _append(self, raw):
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = _directory(directory, self.path.parent, self.failure)
            if parent != self.parent:
                raise self.failure("provider evidence export directory changed before append")
            with self._open(directory) as output:
                before = self._admit(output, directory)
                count = output.write(raw)
                if type(count) is not int or count != len(raw):
                    raise self.failure("provider evidence export write was incomplete or unsupported")
                output.flush()
                opened = os.fstat(output.fileno())
                named = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
                _private(opened, self.failure)
                _private(named, self.failure)
                if _stamp(opened) != _stamp(named) or opened.st_size != before + len(raw):
                    raise self.failure("provider evidence export changed or has incomplete bytes")
                complete = _stamp(opened)
            if _directory(directory, self.path.parent, self.failure) != parent:
                raise self.failure("provider evidence export directory changed during append")
            final = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            _private(final, self.failure)
            if _stamp(final) != complete:
                raise self.failure("provider evidence export changed while closing")
        finally:
            os.close(directory)
        self.identity = complete
