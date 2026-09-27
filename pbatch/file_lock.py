"""Cooperative file locks on POSIX and Windows, with the flock call shape.

POSIX delegates to fcntl. Windows locks one byte at offset 2**62 so its
mandatory byte-range locks do not interfere with ordinary data access.
Windows re-locks release the old range before acquisition: conversions are
not atomic and a failed nonblocking conversion leaves no old lock held.
No descriptors are duplicated or cached; closing the descriptor releases it.

Windows API semantics: https://learn.microsoft.com/en-us/windows/win32/api/
fileapi/nf-fileapi-lockfileex (including shared locks and ranges beyond EOF).
"""

from __future__ import annotations

import ctypes
import errno
import operator
import os
from functools import lru_cache

if os.name == "nt":
    _native = None
else:
    import fcntl as _native

LOCK_SH = _native.LOCK_SH if _native else 1
LOCK_EX = _native.LOCK_EX if _native else 2
LOCK_NB = _native.LOCK_NB if _native else 4
LOCK_UN = _native.LOCK_UN if _native else 8

_ERROR_LOCK_VIOLATION = 33
_ERROR_NOT_LOCKED = 158
_ERROR_IO_PENDING = 997


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", ctypes.c_uint32),
                ("OffsetHigh", ctypes.c_uint32),
                ("hEvent", ctypes.c_void_p)]


class _WindowsAPI:
    def __init__(self, kernel, get_handle, last_error, make_error):
        self.kernel = kernel
        self.get_handle = get_handle
        self.last_error = last_error
        self.make_error = make_error
        handle, word = ctypes.c_void_p, ctypes.c_uint32
        overlap = ctypes.POINTER(_Overlapped)
        signatures = {
            "LockFileEx": [handle, word, word, word, word, overlap],
            "UnlockFileEx": [handle, word, word, word, overlap],
            "GetOverlappedResult": [handle, overlap, ctypes.POINTER(word),
                                    ctypes.c_int],
        }
        for name, arguments in signatures.items():
            function = getattr(kernel, name)
            function.argtypes = arguments
            function.restype = ctypes.c_int

    def flock(self, descriptor: int, operation: int) -> None:
        mode = operation & ~LOCK_NB
        if mode not in (LOCK_SH, LOCK_EX, LOCK_UN):
            raise OSError(errno.EINVAL, "invalid flock operation")
        handle = self.get_handle(descriptor)
        overlap = _Overlapped(OffsetHigh=0x40000000)
        # Prevent stacked shared locks and self-deadlock on repeated EX.
        result = self.kernel.UnlockFileEx(handle, 0, 1, 0,
                                          ctypes.byref(overlap))
        self._complete(result, handle, overlap, allow_unlocked=True)
        if mode == LOCK_UN:
            return
        overlap = _Overlapped(OffsetHigh=0x40000000)
        flags = (2 if mode == LOCK_EX else 0) | (1 if operation & LOCK_NB else 0)
        result = self.kernel.LockFileEx(handle, flags, 0, 1, 0,
                                        ctypes.byref(overlap))
        self._complete(result, handle, overlap)

    def _complete(self, result, handle, overlap, allow_unlocked=False):
        if result:
            return
        error = self.last_error()
        if allow_unlocked and error == _ERROR_NOT_LOCKED:
            return
        if error == _ERROR_IO_PENDING:
            transferred = ctypes.c_uint32()
            if self.kernel.GetOverlappedResult(
                    handle, ctypes.byref(overlap),
                    ctypes.byref(transferred), True):
                return
            error = self.last_error()
        if error == _ERROR_LOCK_VIOLATION:
            raise BlockingIOError(errno.EAGAIN, "file lock is held")
        raise self.make_error(error)


@lru_cache(maxsize=1)
def _windows_api():
    import msvcrt
    return _WindowsAPI(ctypes.WinDLL("kernel32", use_last_error=True),
                       msvcrt.get_osfhandle, ctypes.get_last_error,
                       ctypes.WinError)


def flock(descriptor, operation: int) -> None:
    """Acquire, convert, or release a lock using an int or a fileno object."""
    if _native is not None:
        return _native.flock(descriptor, operation)
    number = descriptor if isinstance(descriptor, int) else descriptor.fileno()
    number = operator.index(number)
    if number < 0:
        raise ValueError("file descriptor cannot be a negative integer")
    _windows_api().flock(number, operator.index(operation))
