"""Expired cooperative admission budgets cannot become zero-wait SQLite writes."""
from contextlib import contextmanager
import fcntl
import math
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from pbatch import llm_pool_state_guard as pool_guard
from pbatch import llm_pool_store as pool
from test_tui_soak_store_guard import guard as fixture_guard


def _clock(monkeypatch, module):
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _: None))
    return clock


def _unlocked(parent):
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("kind", ["fixture", "pool"])
@pytest.mark.parametrize("late_by", [0.0, 1.0])
def test_successful_flock_at_or_after_deadline_refuses_and_closes_guard(kind, late_by, tmp_path, monkeypatch):
    module = fixture_guard if kind == "fixture" else pool_guard
    timeout = 3.0 if kind == "fixture" else 10.0
    clock = _clock(monkeypatch, module)
    native = module.fcntl.flock
    calls = []

    def acquire_late(descriptor, flags):
        native(descriptor, flags)
        calls.append(flags)
        clock[0] += timeout + late_by

    monkeypatch.setattr(module.fcntl, "flock", acquire_late)
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: pytest.fail("expired admission opened SQLite"))
    error = sqlite3.OperationalError if kind == "fixture" else TimeoutError
    context = (fixture_guard.connection(tmp_path / "never.sqlite", timeout) if kind == "fixture"
               else pool_guard.state_guard(tmp_path))
    with pytest.raises(error, match="budget"):
        with context:
            pytest.fail("expired lock became execution admission")
    monkeypatch.setattr(module.fcntl, "flock", native)
    _unlocked(tmp_path)
    assert len(calls) == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["fixture", "pool"])
def test_post_lock_directory_checks_can_exhaust_budget_without_sqlite_or_yield(kind, tmp_path, monkeypatch):
    module = fixture_guard if kind == "fixture" else pool_guard
    timeout = 3.0 if kind == "fixture" else 10.0
    clock = _clock(monkeypatch, module)
    name = "_directory" if kind == "fixture" else "_check_directory"
    check = getattr(module, name)
    checks = []

    def slow_check(*args):
        check(*args)
        checks.append(True)
        if len(checks) == 2:
            clock[0] += timeout

    monkeypatch.setattr(module, name, slow_check)
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: pytest.fail("spent directory check opened SQLite"))
    error = sqlite3.OperationalError if kind == "fixture" else TimeoutError
    context = (fixture_guard.connection(tmp_path / "never.sqlite", timeout) if kind == "fixture"
               else pool_guard.state_guard(tmp_path))
    with pytest.raises(error, match="budget"):
        with context:
            pytest.fail("spent directory check admitted state")
    _unlocked(tmp_path)
    assert checks == [True, True] and not list(tmp_path.iterdir())


@pytest.mark.parametrize("bad", [0, -1, True, False, math.nan, math.inf, -math.inf, None, "3"])
@pytest.mark.parametrize("kind", ["fixture", "pool"])
def test_invalid_wait_budget_refuses_before_clock_or_filesystem(kind, bad, tmp_path, monkeypatch):
    module = fixture_guard if kind == "fixture" else pool_guard
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: pytest.fail("invalid budget read clock")))
    monkeypatch.setattr(module.os, "open", lambda *_a, **_k: pytest.fail("invalid budget opened path"))
    if kind == "pool":
        monkeypatch.setattr(pool_guard, "LOCK_TIMEOUT", bad)
    context = (fixture_guard.connection(tmp_path / "never.sqlite", bad) if kind == "fixture"
               else pool_guard.state_guard(tmp_path))
    with pytest.raises(ValueError, match="finite and positive"):
        with context:
            pytest.fail("invalid wait budget admitted")


def test_pool_private_sidecar_checks_cannot_turn_spent_guard_into_zero_timeout_transaction(tmp_path, monkeypatch):
    store = pool.PoolStore(tmp_path / "owned.sqlite")
    snapshot = store.path.read_bytes()
    now = pool.time.monotonic()

    @contextmanager
    def exhausted(_parent):
        yield now - 1.0

    monkeypatch.setattr(pool, "state_guard", exhausted)
    monkeypatch.setattr(pool.sqlite3, "connect", lambda *_a, **_k: pytest.fail("zero remaining budget opened SQLite"))
    with pytest.raises(pool.PoolStoreError, match="admission refused"):
        with store._transaction(prune=False):
            pytest.fail("expired pool transaction yielded")
    assert store.path.read_bytes() == snapshot


def test_fixture_named_owner_is_also_required_to_match_current_user(tmp_path, monkeypatch):
    native = Path.lstat
    uid = os.getuid()

    def changed_owner(path, *args, **kwargs):
        info = native(path, *args, **kwargs)
        if path == tmp_path:
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev,
                                   st_ino=info.st_ino, st_uid=uid + 1)
        return info

    monkeypatch.setattr(Path, "lstat", changed_owner)
    monkeypatch.setattr(fixture_guard.fcntl, "flock", lambda *_: pytest.fail("foreign named owner locked"))
    with pytest.raises(sqlite3.OperationalError, match="directory unavailable"):
        with fixture_guard.connection(tmp_path / "never.sqlite", 3.0):
            pytest.fail("foreign named owner admitted")
