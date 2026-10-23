"""The cancellation assertion tolerates exited PIDs, never unknown/live ones."""

import errno
from pathlib import Path

import pytest

from test_review_cancellation import _child_inactive


PID = 987654


def _replace_stat(monkeypatch, read):
    original = Path.read_text

    def replacement(path, *args, **kwargs):
        if path == Path(f"/proc/{PID}/stat"):
            return read()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", replacement)


@pytest.mark.parametrize("error", (
    FileNotFoundError(errno.ENOENT, "process was reaped"),
    ProcessLookupError(errno.ESRCH, "process exited during read"),
))
def test_exited_pid_is_inactive_even_when_proc_read_loses_the_race(monkeypatch, error):
    def read():
        raise error

    _replace_stat(monkeypatch, read)
    assert _child_inactive(PID) is True


@pytest.mark.parametrize("state", ("R", "S", "D", "T", "W", "I", "X", "Z"))
def test_only_a_zombie_is_accepted_when_proc_state_is_present(monkeypatch, state):
    _replace_stat(monkeypatch, lambda: f"{PID} (python worker)) {state} 1 2 3")
    assert _child_inactive(PID) is (state == "Z")


@pytest.mark.parametrize("error", (
    PermissionError(errno.EACCES, "cannot observe process"),
    OSError(errno.EIO, "read failed"),
))
def test_unexpected_proc_errors_cannot_hide_an_active_child(monkeypatch, error):
    def read():
        raise error

    _replace_stat(monkeypatch, read)
    with pytest.raises(type(error)) as observed:
        _child_inactive(PID)
    assert observed.value is error


def test_malformed_proc_state_is_not_treated_as_an_exited_child(monkeypatch):
    _replace_stat(monkeypatch, lambda: "invalid proc stat")
    with pytest.raises(IndexError):
        _child_inactive(PID)
