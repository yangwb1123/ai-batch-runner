"""Cooperative ownership of a Hub SQLite file family before any database writes.

The permanent lock inode is never removed. These are local POSIX safeguards,
not isolation from hostile same-UID processes or nonparticipating writers.
"""
from __future__ import annotations

import os
import stat

from . import file_lock
from .hub_store_helpers import HubStoreError


def _unsafe():
    return HubStoreError('unsafe_store_path', 'HubStore storage path is unsafe or changed', 503)


def checked_path(path):
    if len(str(path)) > 4096 or len(os.fsencode(path)) > 4096 or len(path.parts) > 64:
        raise ValueError('HubStore path exceeds storage admission limits')
    return path


def _identity(info):
    return info.st_dev, info.st_ino


def _regular(info, *, private=False):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or (private and info.st_mode & 0o077)):
        raise _unsafe()


def _open_owned(path):
    try:
        before = path.lstat()
        _regular(before)
        flags = os.O_RDWR
    except FileNotFoundError:
        before = None
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        after = os.fstat(descriptor)
        _regular(after)
        if (_identity(path.lstat()) != _identity(after)
                or (before is not None and _identity(before) != _identity(after))):
            raise _unsafe()
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class HubStorageOwner:
    """Pin the database and lock inodes until all admitted connections close."""

    def __init__(self, path):
        self.path = checked_path(path)
        self.lock_path = path.with_name(path.name + '.lock')
        self.database_fd = self.lock_fd = None
        self._parent_identity = None
        self._secured = False

    def _ancestors(self, *, missing=False):
        for directory in (self.path.parent, *self.path.parent.parents):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                if missing:
                    continue
                raise _unsafe()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
                raise _unsafe()

    def _parent(self):
        self._ancestors()
        info = self.path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise _unsafe()
        if self._parent_identity is not None and _identity(info) != self._parent_identity:
            raise _unsafe()
        return _identity(info)

    def _sidecars(self):
        present = False
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                info = self.path.with_name(self.path.name + suffix).lstat()
            except FileNotFoundError:
                continue
            _regular(info, private=True)
            present = True
        return present

    def acquire(self):
        if not all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_NONBLOCK', 'getuid', 'fchmod')):
            raise ValueError('HubStore secure storage requires POSIX no-follow and ownership support')
        self._ancestors(missing=True)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._parent_identity = self._parent()
        self.lock_fd = _open_owned(self.lock_path)
        if os.fstat(self.lock_fd).st_size:
            raise _unsafe()
        try:
            file_lock.flock(self.lock_fd, file_lock.LOCK_EX | file_lock.LOCK_NB)
        except BlockingIOError as exc:
            raise HubStoreError('store_in_use', 'another HubStore owns this database') from exc
        sidecars = self._sidecars()
        if sidecars and not self.path.exists():
            raise _unsafe()
        self.database_fd = _open_owned(self.path)
        if sidecars and not os.fstat(self.database_fd).st_size:
            raise _unsafe()
        self.validate()

    def _pinned(self, path, descriptor):
        if descriptor is None:
            raise _unsafe()
        opened, current = os.fstat(descriptor), path.lstat()
        _regular(opened, private=self._secured)
        _regular(current, private=self._secured)
        if _identity(opened) != _identity(current):
            raise _unsafe()

    def validate(self):
        """Recheck around every SQLite open, without fixing or taking over paths."""
        try:
            self._parent()
            self._pinned(self.lock_path, self.lock_fd)
            if os.fstat(self.lock_fd).st_size:
                raise _unsafe()
            self._pinned(self.path, self.database_fd)
            self._sidecars()
        except OSError as exc:
            raise _unsafe() from exc

    def secure(self):
        """Only a compatible database held by this owner may be restricted."""
        self.validate()
        os.fchmod(self.database_fd, 0o600)
        os.fchmod(self.lock_fd, 0o600)
        self._secured = True
        self.validate()

    def close(self):
        database, self.database_fd = self.database_fd, None
        try:
            if database is not None:
                os.close(database)
        finally:
            lock, self.lock_fd = self.lock_fd, None
            if lock is not None:
                try:
                    file_lock.flock(lock, file_lock.LOCK_UN)
                finally:
                    os.close(lock)
