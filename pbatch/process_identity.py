"""Read-only local PID probes; uncertainty must never authorize stale cleanup."""
from __future__ import annotations

import ctypes
import errno
import os

_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_ERROR_INVALID_PARAMETER = 87


def valid_pid(pid: object) -> bool:
    """Reject coercions and values the native PID type would truncate."""
    maximum = 0xFFFFFFFF if os.name == "nt" else 0x7FFFFFFF
    return type(pid) is int and 0 < pid <= maximum


def pid_alive(pid: object) -> bool:
    """Return False only for invalid PIDs or a process known to have exited.

    Permission failures and unknown OS errors conservatively mean possibly
    alive. Windows os.kill(pid, 0) terminates the process, so it is never used.
    """
    if not valid_pid(pid):
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno != errno.ESRCH
    except (AttributeError, OverflowError, ValueError):
        return True
    return True


def _windows_api():
    """Declare HANDLE as pointer-sized and DWORD/BOOL as fixed-width Win32 types."""
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int32
    return api


def _windows_pid_alive(pid: int) -> bool:
    # OpenProcess(SYNCHRONIZE) + a zero-duration wait never modifies a process.
    # https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject
    try:
        api = _windows_api()
        handle = api.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
        try:
            state = api.WaitForSingleObject(handle, 0)
        finally:
            closed = api.CloseHandle(handle)
        return state != _WAIT_OBJECT_0 or not closed
    except (OSError, AttributeError, OverflowError, ValueError):
        return True
