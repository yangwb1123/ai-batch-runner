"""Fixture writers must offer pollers a window without extra admission budget."""

from contextlib import contextmanager
import fcntl
import os
import sqlite3
from types import SimpleNamespace

import pytest

from test_tui_soak_provider_log import LOG, SoakFailure
from test_tui_soak_store_guard import guard


def test_waiting_reader_gets_an_unlocked_window_before_next_append(tmp_path, monkeypatch):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    first, second = {"number": 1}, {"number": 2}
    LOG.append_provider_event(path, first)
    clock, opportunities, observed = [100.0], [], []

    def poll_waiter(delay):
        opportunities.append(delay)
        clock[0] += delay
        # A previously waiting reader can run at its next poll. In particular,
        # the new writer cannot already own the guard during this interval.
        descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        observed.extend(reader.read())

    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=poll_waiter))
    LOG.append_provider_event(path, second)
    assert opportunities == [.011], "a repeat writer barged without offering a whole poll window"
    assert observed == [first], "the queued read must precede the next write"
    assert reader.read() == [second] and reader.offset == 2
    assert LOG.LOCK_TIMEOUT == 3.0


def test_handoff_is_charged_to_same_three_second_sqlite_budget(tmp_path, monkeypatch):
    clock, sleeps, timeouts = [100.0], [], []
    connect = sqlite3.connect

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    def observed_connect(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return connect(*args, **kwargs)

    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(guard.sqlite3, "connect", observed_connect)
    with guard.connection(tmp_path / "owned.sqlite", LOG.LOCK_TIMEOUT, writer=True):
        pass
    assert sleeps == [.011]
    assert timeouts == [pytest.approx(3.0 - .011)]
    assert LOG.LOCK_TIMEOUT == 3.0


@pytest.mark.parametrize("timeout,elapsed", [(.005, .005), (3.0, 3.0), (3.0, 4.0)])
def test_expired_handoff_refuses_before_filesystem_or_sqlite(timeout, elapsed, tmp_path, monkeypatch):
    clock, sleeps = [100.0], []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += elapsed  # includes a descheduled writer, not just requested sleep

    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(guard.os, "open", lambda *_a, **_k: pytest.fail("expired writer opened path"))
    monkeypatch.setattr(guard.sqlite3, "connect", lambda *_a, **_k: pytest.fail("expired writer opened SQLite"))
    with pytest.raises(sqlite3.OperationalError, match="budget"):
        with guard.connection(tmp_path / "never.sqlite", timeout, writer=True):
            pytest.fail("expired handoff admitted a writer")
    assert sleeps == [min(.011, timeout)]


def test_writer_handoff_does_not_hide_a_permanently_held_guard(tmp_path, monkeypatch):
    clock, sleeps = [0.0], []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    def busy(*_args):
        raise BlockingIOError("controlled permanent holder")

    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(guard.fcntl, "flock", busy)
    monkeypatch.setattr(guard.sqlite3, "connect", lambda *_a, **_k: pytest.fail("busy writer opened SQLite"))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with guard.connection(tmp_path / "never.sqlite", 3.0, writer=True):
            pytest.fail("a permanent holder was bypassed")
    assert clock[0] == pytest.approx(3.0)
    assert sleeps[0] == .011 and max(sleeps) <= .011
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("bad", [0, 1, None, "true", [], {}])
def test_writer_policy_requires_a_boolean_before_clock_or_io(bad, tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: pytest.fail("bad policy read clock")))
    monkeypatch.setattr(guard.os, "open", lambda *_a, **_k: pytest.fail("bad policy opened directory"))
    with pytest.raises(ValueError, match="boolean"):
        with guard.connection(tmp_path / "never.sqlite", 3.0, writer=bad):
            pytest.fail("bad policy admitted a connection")


def test_reader_has_no_added_handoff_and_durability_settings_stay_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    LOG.append_provider_event(path, {"kind": "durable"})
    monkeypatch.setattr(guard, "time", SimpleNamespace(
        monotonic=lambda: 100.0, sleep=lambda *_: pytest.fail("uncontended reader was delayed")))
    assert reader.read() == [{"kind": "durable"}]
    with sqlite3.connect(LOG.database_path(path)) as store:
        assert store.execute("PRAGMA synchronous").fetchone() == (2,)
        assert store.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert store.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)


def test_only_append_requests_writer_handoff(tmp_path, monkeypatch):
    original, modes = LOG.connection, []

    @contextmanager
    def observed(path, timeout, **kwargs):
        modes.append(kwargs.get("writer", False))
        with original(path, timeout, **kwargs) as store:
            yield store

    monkeypatch.setattr(LOG, "connection", observed)
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    LOG.append_provider_event(path, {"kind": "one"})
    assert reader.read() == [{"kind": "one"}]
    assert modes == [False, True, False]
