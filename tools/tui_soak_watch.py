"""Bounded Linux hints for new journals in an already observed local scope.

The harness starts with a fresh evidence directory. Its first journal installs
the scope watch before session.created reaches the caller, so subsequent /new
operations need not wait behind historical tree traversal. These are discovery
hints only: the journal reader validates the bytes and finish scans all files.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import struct
import sys


_CREATE, _MOVED_TO, _ISDIR = 0x100, 0x80, 0x40000000
_INVALIDATED, _OVERFLOW = 0x400 | 0x800 | 0x8000, 0x4000
_WATCH_FLAGS = _CREATE | _MOVED_TO | _INVALIDATED | 0x01000000 | 0x02000000
_HEADER = struct.Struct("iIII")


class JournalHints:
    MAX_SCOPES, MAX_PENDING, READ_BYTES = 8, 16, 65536

    def __init__(self, root: Path, failure):
        self.root, self.failure = root.resolve(), failure
        self.fd, self.library = -1, None
        self.scopes, self.pending = {}, set()

    def observe(self, path: Path) -> None:
        """Only validated local journal layout establishes a watched scope."""
        if not sys.platform.startswith("linux"):
            return
        scope = self._scope(path)
        if scope is None or scope in self.scopes.values():
            return
        if len(self.scopes) >= self.MAX_SCOPES:
            raise self.failure("journal hints exceeded their scope limit")
        self._open()
        descriptor = self.library.inotify_add_watch(self.fd, os.fsencode(scope), _WATCH_FLAGS)
        if descriptor < 0:
            raise OSError(ctypes.get_errno(), "cannot watch local journal scope", str(scope))
        self.scopes[descriptor] = scope
        self._backfill(scope, path)

    def _scope(self, path: Path) -> Path | None:
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            return None
        if (len(parts) != 7 or parts[0] != "projects" or parts[2:4] != ("tui", "events")
                or parts[-1] != "events.jsonl"
                or any(re.fullmatch("[0-9a-f]{24}", value) is None for value in parts[4:6])):
            return None
        return path.parent.parent

    def _open(self) -> None:
        if self.fd >= 0:
            return
        self.library = ctypes.CDLL(None, use_errno=True)
        self.library.inotify_init1.argtypes = [ctypes.c_int]
        self.library.inotify_init1.restype = ctypes.c_int
        self.library.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.library.inotify_add_watch.restype = ctypes.c_int
        self.fd = self.library.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "cannot initialize journal hints")

    def _backfill(self, scope: Path, observed: Path) -> None:
        # Watch first, then backfill: creation during installation cannot vanish
        # between a directory snapshot and registration. Fresh harness output
        # bounds this initial sibling set; historical scopes are not reopened.
        with os.scandir(scope) as entries:
            for count, entry in enumerate(entries):
                if count >= 64:
                    raise self.failure("initial journal scope exceeded its backfill limit")
                if entry.is_dir(follow_symlinks=False):
                    path = Path(entry.path) / "events.jsonl"
                    if path != observed:
                        self._hint(scope, entry.name)

    def _hint(self, scope: Path, name: str) -> None:
        if re.fullmatch("[0-9a-f]{24}", name) is None:
            return
        self.pending.add(scope / name / "events.jsonl")
        if len(self.pending) > self.MAX_PENDING:
            raise self.failure("journal hints exceeded their pending-session limit")

    def poll(self) -> tuple[Path, ...]:
        if self.fd >= 0:
            try:
                value = os.read(self.fd, self.READ_BYTES)
            except BlockingIOError:
                value = b""
            self._consume(value)
        return tuple(self.pending)

    def _consume(self, value: bytes) -> None:
        offset = 0
        while offset < len(value):
            if len(value) - offset < _HEADER.size:
                raise self.failure("truncated journal notification header")
            descriptor, mask, _cookie, size = _HEADER.unpack_from(value, offset)
            offset += _HEADER.size
            if size > len(value) - offset:
                raise self.failure("truncated journal notification name")
            name = os.fsdecode(value[offset:offset + size].split(b"\0", 1)[0])
            offset += size
            if mask & (_OVERFLOW | _INVALIDATED):
                raise self.failure("journal notification overflow or watched scope changed")
            scope = self.scopes.get(descriptor)
            if scope is not None and mask & (_CREATE | _MOVED_TO) and mask & _ISDIR:
                self._hint(scope, name)

    def acknowledge(self, path: Path) -> None:
        self.pending.discard(path)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
