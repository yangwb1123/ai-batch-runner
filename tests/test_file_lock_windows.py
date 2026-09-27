"""Win32 API boundary simulations; these are not native Windows evidence."""

from __future__ import annotations

import ctypes
import errno
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pbatch import file_lock


class _Kernel:
    def __init__(self):
        self.error = 0
        self.locks = {}
        self.ranges = []
        self.LockFileEx = Mock(side_effect=self._lock)
        self.UnlockFileEx = Mock(side_effect=self._unlock)
        self.GetOverlappedResult = Mock(return_value=1)

    def _lock(self, handle, flags, reserved, low, high, overlap):
        record = overlap._obj
        self.ranges.append((record.Offset, record.OffsetHigh, low, high))
        exclusive = bool(flags & 2)
        existing = [mode for modes in self.locks.values() for mode in modes]
        if existing and (exclusive or any(existing)):
            self.error = 33
            return 0
        self.locks.setdefault(handle, []).append(exclusive)
        return 1

    def _unlock(self, handle, reserved, low, high, overlap):
        held = self.locks.get(handle)
        if not held:
            self.error = 158
            return 0
        held.pop()
        return 1


def _error(code):
    error = OSError(errno.EBADF, "simulated Windows error")
    error.winerror = code
    return error


@pytest.fixture
def win_api(monkeypatch):
    kernel = _Kernel()
    api = file_lock._WindowsAPI(kernel, lambda fd: fd + (1 << 40),
                                lambda: kernel.error, _error)
    monkeypatch.setattr(file_lock, "_native", None)
    monkeypatch.setattr(file_lock, "_windows_api", lambda: api)
    return kernel


@pytest.mark.parametrize("mode, flags", [(1, 0), (2, 2), (5, 1), (6, 3)])
def test_windows_flags_handle_width_and_safe_range(win_api, mode, flags):
    file_lock.flock(SimpleNamespace(fileno=lambda: 7), mode)
    args = win_api.LockFileEx.call_args.args
    assert args[:5] == (7 + (1 << 40), flags, 0, 1, 0)
    assert win_api.ranges == [(0, 0x40000000, 1, 0)]
    assert win_api.LockFileEx.argtypes[0] is ctypes.c_void_p
    assert ctypes.sizeof(file_lock._Overlapped) == (32 if ctypes.sizeof(
        ctypes.c_void_p) == 8 else 20)


@pytest.mark.parametrize("mode", [1, 2])
def test_windows_relocking_does_not_stack(win_api, mode):
    file_lock.flock(7, mode | 4)
    file_lock.flock(7, mode | 4)
    file_lock.flock(7, 8)
    file_lock.flock(8, 6)
    assert win_api.locks[7 + (1 << 40)] == []


def test_windows_conversions_and_failed_upgrade_release_old_lock(win_api):
    file_lock.flock(7, 2)
    file_lock.flock(7, 1)
    file_lock.flock(8, 1)
    with pytest.raises(BlockingIOError) as caught:
        file_lock.flock(7, 6)
    assert caught.value.errno == errno.EAGAIN
    file_lock.flock(8, 8)
    file_lock.flock(9, 6)


def test_windows_unlock_unheld_is_noop_and_invalid_mode_preserves_lock(win_api):
    file_lock.flock(7, 8)
    file_lock.flock(7, 2)
    with pytest.raises(OSError) as caught:
        file_lock.flock(7, 3)
    assert caught.value.errno == errno.EINVAL
    with pytest.raises(BlockingIOError):
        file_lock.flock(8, 6)


def test_windows_api_errors_are_not_silenced(win_api):
    win_api.UnlockFileEx.side_effect = None
    win_api.UnlockFileEx.return_value = 0
    win_api.error = 6
    with pytest.raises(OSError) as caught:
        file_lock.flock(7, 2)
    assert caught.value.winerror == 6
    win_api.LockFileEx.assert_not_called()


def test_windows_pending_lock_waits_for_completion(win_api):
    win_api.LockFileEx.side_effect = None
    win_api.LockFileEx.return_value = 0
    win_api.UnlockFileEx.side_effect = None
    win_api.UnlockFileEx.return_value = 1
    win_api.error = 997
    file_lock.flock(7, 2)
    assert win_api.GetOverlappedResult.call_args.args[-1] is True


def test_windows_negative_descriptor_fails_before_api(win_api):
    with pytest.raises(ValueError, match="negative"):
        file_lock.flock(-1, 2)
    win_api.UnlockFileEx.assert_not_called()
