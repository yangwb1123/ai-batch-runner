"""PID probes cannot send Windows termination signals or authorize unknown death."""
from __future__ import annotations

import ctypes
import errno
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pbatch import campaign_watchdog, process_identity, registry, triage


def _platform(monkeypatch, name, kill=None):
    kill = kill or Mock(side_effect=AssertionError("PID probe attempted a signal"))
    monkeypatch.setattr(process_identity, "os", SimpleNamespace(name=name, kill=kill))
    return kill


@pytest.mark.parametrize("platform", ["posix", "nt"])
@pytest.mark.parametrize("pid", [
    None, True, False, 0, -1, -123, "123", "-1", 1.0, float("inf"),
    float("nan"), [], {}, 2 ** 64, 10 ** 1000,
])
def test_invalid_pids_never_reach_native_probe(monkeypatch, platform, pid):
    kill = _platform(monkeypatch, platform)
    windows = Mock(side_effect=AssertionError("invalid PID reached Win32"))
    monkeypatch.setattr(process_identity, "_windows_pid_alive", windows)
    assert process_identity.valid_pid(pid) is False
    assert process_identity.pid_alive(pid) is False
    kill.assert_not_called()
    windows.assert_not_called()


@pytest.mark.parametrize("platform, maximum", [("posix", 0x7FFFFFFF), ("nt", 0xFFFFFFFF)])
def test_native_pid_width_rejects_truncation(monkeypatch, platform, maximum):
    _platform(monkeypatch, platform)
    assert process_identity.valid_pid(maximum)
    assert not process_identity.valid_pid(maximum + 1)
    assert not process_identity.pid_alive(maximum + 1)


@pytest.mark.parametrize("error, expected", [
    (None, True), (OSError(errno.ESRCH, "gone"), False),
    (OSError(errno.EPERM, "permission denied"), True),
    (OSError(errno.EACCES, "access denied"), True),
    (OSError(errno.EIO, "unknown I/O state"), True),
    (OSError(errno.EINTR, "interrupted"), True),
    (OSError("unknown errno"), True), (ProcessLookupError("unknown errno"), True),
    (ValueError("unknown platform state"), True),
])
def test_posix_only_esrch_proves_death(monkeypatch, error, expected):
    kill = _platform(monkeypatch, "posix", Mock(side_effect=error))
    assert process_identity.pid_alive(123) is expected
    kill.assert_called_once_with(123, 0)


def _windows(monkeypatch, *, handle=0x123456789ABC, wait=258, error=0):
    _platform(monkeypatch, "nt")
    api = SimpleNamespace(OpenProcess=Mock(return_value=handle),
                          WaitForSingleObject=Mock(return_value=wait),
                          CloseHandle=Mock(return_value=1))
    loader = Mock(return_value=api)
    monkeypatch.setattr(ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    return api, loader


@pytest.mark.parametrize("state, expected", [(0, False), (258, True), (0xFFFFFFFF, True),
                                           (128, True), (12345, True)])
def test_windows_wait_is_read_only_bounded_and_always_closes(monkeypatch, state, expected):
    api, loader = _windows(monkeypatch, wait=state)
    assert process_identity.pid_alive(123) is expected
    loader.assert_called_once_with("kernel32", use_last_error=True)
    api.OpenProcess.assert_called_once_with(0x00100000, False, 123)
    api.WaitForSingleObject.assert_called_once_with(0x123456789ABC, 0)
    api.CloseHandle.assert_called_once_with(0x123456789ABC)
    assert api.OpenProcess.restype is ctypes.c_void_p
    assert api.OpenProcess.argtypes == [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    assert api.WaitForSingleObject.argtypes == [ctypes.c_void_p, ctypes.c_uint32]
    assert api.WaitForSingleObject.restype is ctypes.c_uint32
    assert api.CloseHandle.argtypes == [ctypes.c_void_p]
    assert api.CloseHandle.restype is ctypes.c_int32


@pytest.mark.parametrize("error, expected", [(87, False), (5, True), (6, True), (8, True), (0, True)])
def test_windows_open_failure_never_falls_back_to_signals(monkeypatch, error, expected):
    api, _loader = _windows(monkeypatch, handle=None, error=error)
    assert process_identity.pid_alive(123) is expected
    api.WaitForSingleObject.assert_not_called()
    api.CloseHandle.assert_not_called()


@pytest.mark.parametrize("stage", ["load", "wait", "close", "close_failed"])
def test_windows_native_errors_are_conservative_and_close_owned_handles(monkeypatch, stage):
    api, loader = _windows(monkeypatch, wait=0)
    if stage == "load":
        loader.side_effect = OSError("Win32 query unavailable")
    elif stage == "wait":
        api.WaitForSingleObject.side_effect = OSError("query failed")
    elif stage == "close":
        api.CloseHandle.side_effect = OSError("close failed")
    else:
        api.CloseHandle.return_value = 0
    assert process_identity.pid_alive(123) is True
    if stage != "load":
        api.CloseHandle.assert_called_once_with(0x123456789ABC)


def test_existing_wrappers_use_shared_probe_and_preserve_pid_identity(monkeypatch):
    probe = Mock(return_value=True)
    monkeypatch.setattr(process_identity, "pid_alive", probe)
    monkeypatch.setattr(campaign_watchdog, "process_start_ticks", lambda _pid: "birth")
    assert registry._alive(123) and triage.pid_alive(123)
    assert campaign_watchdog._pid_is_same_process({"pid": 123, "process_start_ticks": "birth"})
    assert not campaign_watchdog._pid_is_same_process({"pid": 123, "process_start_ticks": "old"})
    assert probe.call_count == 4


def test_unknown_process_state_prevents_campaign_lease_takeover(tmp_path, monkeypatch):
    monkeypatch.setattr(process_identity, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(campaign_watchdog, "process_start_ticks", lambda _pid: "")
    path = tmp_path / "lease.json"
    path.write_text('{"pid": 123, "schema": 1}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="already held"):
        campaign_watchdog.prepare_lease(
            path, pid=456, direction_id="new", worktree=tmp_path,
            pipeline=tmp_path / "pipeline", logfile=tmp_path / "log",
            timeout=10, no_progress_timeout=10)
    assert path.read_text() == '{"pid": 123, "schema": 1}'


def test_native_live_child_survives_all_probes_and_exit_is_detected():
    # No POSIX skip: native Windows CI executes this against the real Win32 API.
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        for _attempt in range(10):
            assert process_identity.pid_alive(child.pid)
            assert registry._alive(child.pid) and triage.pid_alive(child.pid)
            assert campaign_watchdog._pid_is_same_process({"pid": child.pid})
            assert child.poll() is None, "read-only PID probe terminated the child"
        child.communicate(input=b"q", timeout=10)
        assert child.returncode == 0
        assert not process_identity.pid_alive(child.pid)
        assert not registry._alive(child.pid) and not triage.pid_alive(child.pid)
        assert not campaign_watchdog._pid_is_same_process({"pid": child.pid})
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if child.stdin is not None:
            child.stdin.close()
