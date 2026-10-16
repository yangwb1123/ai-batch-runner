"""Periodic availability must not alias every fixed poll to a busy instant."""
from types import SimpleNamespace

import pytest

from test_tui_soak_store_guard import guard


def _periodic_holder(monkeypatch, busy_until_ns=1_000_000):
    clock, sleeps, attempts = [0], [], []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += round(delay * 1_000_000_000)

    def flock(_descriptor, _mode):
        attempts.append(clock[0])
        # A controlled repeated holder leaves the middle of each 10ms period
        # free. Nothing depends on actual host scheduling or file contents.
        phase = clock[0] % 10_000_000
        if phase < busy_until_ns or phase >= 9_000_000:
            raise BlockingIOError("owned periodic contention")

    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: clock[0] / 1e9, sleep=sleep))
    monkeypatch.setattr(guard.fcntl, "flock", flock)
    return clock, sleeps, attempts


def test_poll_phase_can_drift_into_available_window_within_original_budget(monkeypatch):
    clock, sleeps, attempts = _periodic_holder(monkeypatch)
    guard._lock(123, 3.0)
    assert 0 < clock[0] < 3_000_000_000
    assert 1_000_000 <= attempts[-1] % 10_000_000 < 9_000_000
    assert sleeps[0] == .01
    assert len(attempts) < 20 and all(delay >= .01 for delay in sleeps)


def test_later_available_phase_is_reached_without_more_frequent_polling(monkeypatch):
    clock, sleeps, attempts = _periodic_holder(monkeypatch, busy_until_ns=8_000_000)
    guard._lock(123, 3.0)
    assert 0 < clock[0] < 3_000_000_000 and len(attempts) < 40
    assert all(.01 <= delay <= .011 for delay in sleeps)
    assert attempts[-1] % 10_000_000 >= 8_000_000


def test_phase_drift_never_extends_or_restarts_short_remaining_deadline(monkeypatch):
    clock, sleeps, attempts = _periodic_holder(monkeypatch)
    with pytest.raises(guard.sqlite3.OperationalError, match="budget|locked"):
        guard._lock(123, .0105)
    assert clock[0] == 10_500_000
    assert attempts == [0, 10_000_000, 10_500_000]
    assert sleeps == [.01, pytest.approx(.0005)]


def test_permanently_busy_guard_retains_original_three_second_bound(monkeypatch):
    clock, sleeps = [0], []

    def busy(*_args):
        raise BlockingIOError("owned permanently held guard")

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += round(delay * 1_000_000_000)

    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: clock[0] / 1e9, sleep=sleep))
    monkeypatch.setattr(guard.fcntl, "flock", busy)
    with pytest.raises(guard.sqlite3.OperationalError, match="locked"):
        guard._lock(123, 3.0)
    assert clock[0] == 3_000_000_000 and len(sleeps) <= 300
    assert 0 < sleeps[-1] <= .011
