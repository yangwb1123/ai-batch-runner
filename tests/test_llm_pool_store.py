"""Persistent account-pool admission, recovery and private state boundaries."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import llm_pool_store as module
from pbatch.llm_pool_store import Lease, PoolAccount, PoolStore, PoolStoreError


def account(name="a", group="g", cap=1, **kwargs):
    return PoolAccount(name, "/unread/credential-secret", group, max_concurrent=cap, **kwargs)


def failure(kind="quota", retry_at=None, window="5h", scope="model"):
    return SimpleNamespace(kind=kind, code="private response content", retry_at=retry_at,
                           window=window, scope=scope, body="never persist this")


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: value[0])
    return value


@pytest.fixture
def store(tmp_path):
    return PoolStore(tmp_path / "private" / "pool.sqlite")


def test_fill_first_priority_input_order_and_group_exclusion(store):
    accounts = [account("z", "first", 2), account("a", "second"),
                account("urgent", "urgent", priority=-1)]
    first = store.acquire(accounts, "m", 60)
    assert first.account_id == "urgent"
    second = store.acquire(accounts, "m", 60)
    third = store.acquire(accounts, "other-model", 60)
    assert second.account_id == third.account_id == "z"
    assert store.acquire(accounts, "m", 60, {"second"}) is None
    assert store.acquire(accounts, "m", 60).account_id == "a"
    store.finish(first)
    assert store.acquire(accounts, "m", 60).account_id == "urgent"
    with pytest.raises(FrozenInstanceError):
        accounts[0].id = "changed"
    with pytest.raises(FrozenInstanceError):
        first.id = "changed"


def test_group_minimum_includes_disabled_keys_and_all_models(store, clock):
    accounts = [account(cap=4), account("b", cap=1, enabled=False)]
    lease = store.acquire(accounts, "m", 123)
    assert lease.expires_at == 1123
    assert store.acquire(accounts, "other", 60) is None
    status = store.status(accounts, "m")
    assert status["in_flight"] == 1
    assert status["next_available_at"] == 1123
    assert status["accounts"][1]["state"] == "disabled"
    store.finish(lease)
    assert store.acquire(accounts, "other", 60)


def test_threads_share_atomic_admission_and_release(store):
    accounts = [account(cap=2), account("b", cap=2)]
    barrier = threading.Barrier(16)

    def reserve(index):
        local = store if index % 2 else PoolStore(store.path)
        barrier.wait(timeout=10)
        return local.acquire(accounts, "m", 60)

    with ThreadPoolExecutor(max_workers=16) as executor:
        admitted = [lease for lease in executor.map(reserve, range(16)) if lease]
    assert len(admitted) == 2
    assert {lease.account_id for lease in admitted} == {"a"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(store.finish, admitted))
    assert store.status(accounts)["in_flight"] == 0
    assert store.acquire(accounts, "m", 60)


def test_privacy_checks_preserve_sqlite_lock_against_other_processes(store):
    # POSIX closing *any* descriptor for a DB drops this process's record locks.
    script = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0.05)
try:
    connection.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as error:
    assert 'locked' in str(error)
    sys.exit(0)
sys.exit(1)
"""
    with store._transaction():
        module._private_file(store.path)
        result = subprocess.run([sys.executable, "-c", script, str(store.path)],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr


_PROCESS = """
import json, sys
from dataclasses import asdict
from pathlib import Path
from pbatch.llm_pool_store import PoolAccount, PoolStore
print('ready', flush=True)
sys.stdin.readline()
store = PoolStore(Path(sys.argv[1]))
accounts = [PoolAccount('a', '/not/read', 'g', max_concurrent=2),
            PoolAccount('b', '/not/read', 'g', max_concurrent=2)]
lease = store.acquire(accounts, 'm', 60)
print(json.dumps(asdict(lease) if lease else None), flush=True)
"""


def test_subprocesses_share_initialization_and_admission_after_exit(tmp_path):
    path = tmp_path / "process-state" / "pool.sqlite"
    root = Path(__file__).resolve().parents[1]
    workers = [subprocess.Popen([sys.executable, "-c", _PROCESS, str(path)], cwd=root,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True) for _ in range(8)]
    try:
        for worker in workers:
            assert worker.stdout.readline().strip() == "ready"
        for worker in workers:
            worker.stdin.write("go\n")
            worker.stdin.flush()
        records = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            assert worker.returncode == 0, stderr
            records.append(json.loads(stdout))
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)
    admitted = [Lease(**record) for record in records if record]
    assert len(admitted) == 2
    store = PoolStore(path)
    assert store.acquire([account(cap=2)], "m", 60) is None
    store.finish(admitted[0])
    assert store.acquire([account(cap=2)], "m", 60)


@pytest.mark.parametrize("kind", ["quota", "rate", "rate_limit"])
def test_failure_shares_group_cooldown_but_isolates_other_groups_and_models(store, clock, kind):
    accounts = [account(), account("sibling"), account("other", "separate")]
    lease = store.acquire(accounts, "m", 300)
    store.finish(lease, failure(kind, retry_at=1200))
    reopened = PoolStore(store.path)
    assert reopened.acquire(accounts[:2], "m", 60) is None
    independent = reopened.acquire(accounts, "m", 60)
    assert independent.account_id == "other"
    other_model = reopened.acquire(accounts, "different", 60)
    assert other_model.account_id == "a"
    block = reopened.status(accounts, "m")["accounts"][1]["blocks"][0]
    assert block == {"model": "m", "window": "5h", "retry_at": 1200, "reset_known": True}


def test_account_scope_blocks_every_model_and_windows_recover_independently(store, clock):
    accounts = [account(cap=2), account("b", cap=2)]
    short = store.acquire(accounts, "m", 300)
    weekly = store.acquire(accounts, "m", 300)
    store.finish(short, failure(retry_at=1100, scope="account"))
    store.finish(weekly, failure(retry_at=5000, window="weekly", scope="account"))
    assert store.acquire(accounts, "different", 60) is None
    clock[0] = 1101
    reopened = PoolStore(store.path)
    status = reopened.status(accounts, "m")
    assert status["next_available_at"] == 5000
    assert [block["window"] for block in status["accounts"][0]["blocks"]] == ["weekly"]
    assert reopened.acquire(accounts, "m", 60) is None
    clock[0] = 5000
    assert reopened.acquire(accounts, "different", 60)


def test_success_and_shorter_concurrent_failures_never_clear_newer_cooldown(store, clock):
    accounts = [account(cap=3)]
    success, long, short = [store.acquire(accounts, "m", 300) for _ in range(3)]
    store.finish(long, failure(retry_at=1400))
    store.finish(success)
    store.finish(short, failure(retry_at=1100))
    assert store.status(accounts, "m")["next_available_at"] == 1400
    assert store.acquire(accounts, "m", 60) is None


def test_unknown_reset_waits_for_single_exclusive_probe_and_success(store, clock):
    accounts = [account(cap=3), account("b", cap=3)]
    lease = store.acquire(accounts, "m", 300)
    store.finish(lease, failure(), probe_after=60)
    before = store.status(accounts, "m")["accounts"][0]
    assert before["blocks"][0]["reset_known"] is False
    assert before["next_available_at"] == 1060
    assert store.acquire(accounts, "m", 300) is None
    clock[0] = 1060
    reopened = PoolStore(store.path)
    assert reopened.status(accounts, "m")["accounts"][0]["state"] == "probe_due"
    probe = reopened.acquire(accounts, "m", 300)
    assert reopened.acquire(accounts, "m", 300) is None
    assert reopened.acquire(accounts, "other-model", 300) is None
    reopened.finish(probe)
    assert reopened.status(accounts, "m")["accounts"][0]["blocks"] == []
    assert all(reopened.acquire(accounts, "m", 300) for _ in range(3))


def test_unknown_probe_waits_for_other_models_and_preserves_unrelated_windows(store, clock):
    accounts = [account(cap=3)]
    unknown, weekly, running = [store.acquire(accounts, model, 300) for model in ("m", "x", "y")]
    store.finish(unknown, failure())
    store.finish(weekly, failure(retry_at=5000, window="weekly"))
    clock[0] = 1060
    assert store.acquire(accounts, "m", 300) is None
    store.finish(running)
    probe = store.acquire(accounts, "m", 300)
    store.finish(probe)
    assert store.acquire(accounts, "x", 300) is None
    assert store.status(accounts, "x")["next_available_at"] == 5000


@pytest.mark.parametrize("probe_after,expected", [(0.1, 1001), (60, 1060), (99999, 4600)])
def test_unknown_probe_interval_is_bounded_without_claiming_reset(store, clock, probe_after, expected):
    accounts = [account()]
    store.finish(store.acquire(accounts, "m", 60), failure(), probe_after=probe_after)
    block = store.status(accounts, "m")["accounts"][0]["blocks"][0]
    assert block["retry_at"] == expected
    assert block["reset_known"] is False


def test_expired_probe_and_duplicate_success_cannot_erase_new_failure(store, clock):
    accounts = [account(cap=2)]
    store.finish(store.acquire(accounts, "m", 60), failure(), probe_after=1)
    clock[0] = 1001
    old = store.acquire(accounts, "m", 10)
    clock[0] = 1011
    probe = store.acquire(accounts, "m", 60)
    store.finish(probe, failure(retry_at=5000))
    store.finish(old)
    store.finish(probe)
    assert store.acquire(accounts, "m", 60) is None
    assert store.status(accounts, "m")["next_available_at"] == 5000


def test_concurrent_success_preserves_unknown_failure_and_failed_probe_renews(store, clock):
    accounts = [account(cap=2)]
    success, exhausted = [store.acquire(accounts, "m", 300) for _ in range(2)]
    store.finish(exhausted, failure())
    store.finish(success)
    clock[0] = 1060
    assert store.status(accounts, "m")["accounts"][0]["state"] == "probe_due"
    probe = store.acquire(accounts, "m", 300)
    store.finish(probe, failure(), probe_after=20)
    block = store.status(accounts, "m")["accounts"][0]["blocks"][0]
    assert block["retry_at"] == 1080
    assert block["reset_known"] is False
    assert store.acquire(accounts, "m", 300) is None


@pytest.mark.parametrize("kind", ["service", "request"])
def test_service_request_only_release_and_failed_probe_remains_unknown(store, clock, kind):
    accounts = [account()]
    store.finish(store.acquire(accounts, "m", 60), failure(kind))
    lease = store.acquire(accounts, "m", 60)
    assert lease.account_id == "a"
    store.finish(lease, failure(), probe_after=1)
    clock[0] = 1001
    store.finish(store.acquire(accounts, "m", 60), failure(kind))
    state = store.status(accounts, "m")["accounts"][0]
    assert state["state"] == "probe_due"
    assert state["blocks"][0]["reset_known"] is False


def test_admin_pause_auth_reset_and_quota_state_are_independent(store, clock):
    accounts = [account(cap=2), account("b", cap=2)]
    auth, quota = [store.acquire(accounts, "m", 60) for _ in range(2)]
    store.set_enabled("a", False)
    store.finish(auth, failure("auth"))
    store.finish(quota, failure(retry_at=1200))
    store = PoolStore(store.path)
    store.set_enabled("a", True)
    assert store.status(accounts, "other")["accounts"][0]["state"] == "auth_failed"
    sibling = store.acquire(accounts, "other", 60)
    assert sibling.account_id == "b"
    store.finish(sibling)
    store.set_enabled("a", False)
    store.reset_auth("a")
    assert store.status(accounts, "m")["accounts"][0]["state"] == "disabled"
    store.set_enabled("a", True)
    assert store.acquire(accounts, "m", 60) is None
    clock[0] = 1200
    assert PoolStore(store.path).acquire(accounts, "m", 60).account_id == "a"


def test_pause_before_configuration_survives_reopen_and_expiry_prunes(store, clock):
    accounts = [account()]
    store.set_enabled("a", False)
    store.reset_auth("a")
    store = PoolStore(store.path)
    assert store.acquire(accounts, "m", 60) is None
    store.set_enabled("a", True)
    lease = store.acquire(accounts, "m", 10)
    store.finish(replace(lease, quota_group="forged"), failure("auth"))
    assert store.status(accounts, "m")["in_flight"] == 1
    clock[0] = 1010
    replacement = PoolStore(store.path).acquire(accounts, "m", 60)
    assert replacement.id != lease.id
    store.finish(lease, failure("auth"))
    assert store.status(accounts, "m")["in_flight"] == 1


def test_redaction_event_bound_and_private_permissions(store):
    accounts = [account()]
    store.finish(store.acquire(accounts, "m", 60), failure("request"))
    for index in range(300):
        store.set_enabled("a", bool(index % 2))
    assert len(store.events(9999)) == 256
    assert len(store.events(3)) == 3
    assert store.events(0) == []
    serialized = json.dumps([store.status(accounts), store.events()])
    with sqlite3.connect(store.path) as connection:
        serialized += "\n".join(connection.iterdump())
    serialized += store.path.read_bytes().decode("latin-1")
    for secret in ("credential-secret", "private response content", "never persist this"):
        assert secret not in serialized
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    store.path.chmod(0o666)
    store.path.parent.chmod(0o777)
    store.status(accounts)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("payload", [b"", b"not sqlite", b"SQLite format 3\x00" + b"broken" * 100])
def test_corrupt_existing_database_is_not_reinitialized(tmp_path, payload):
    path = tmp_path / "pool.sqlite"
    path.write_bytes(payload)
    with pytest.raises(PoolStoreError):
        PoolStore(path)
    assert path.read_bytes() == payload


@pytest.mark.parametrize("statement", ["PRAGMA user_version=99", "DROP TABLE blocks",
                                       "CREATE TABLE unexpected (value TEXT)"])
def test_incompatible_schema_fails_existing_store_and_reopen(store, statement):
    with sqlite3.connect(store.path) as connection:
        connection.execute(statement)
    with pytest.raises(PoolStoreError):
        store.acquire([account()], "m", 60)
    with pytest.raises(PoolStoreError):
        PoolStore(store.path)


@pytest.mark.parametrize("target", ["database", "directory", "ancestor", "sidecar"])
def test_symlink_state_is_refused_without_touching_target(tmp_path, target):
    real = tmp_path / "real"
    real.mkdir()
    victim = real / "victim"
    victim.write_text("untouched")
    state = tmp_path / "state"
    state.mkdir()
    path = state / "pool.sqlite"
    if target == "database":
        path.symlink_to(victim)
    elif target in ("directory", "ancestor"):
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        path = alias / ("nested/pool.sqlite" if target == "ancestor" else "pool.sqlite")
    else:
        PoolStore(path)
        Path(str(path) + "-journal").symlink_to(victim)
    with pytest.raises(PoolStoreError):
        PoolStore(path)
    assert victim.read_text() == "untouched"
    assert not (real / "nested").exists()


def test_owner_mismatch_fails_closed(store, monkeypatch):
    if not hasattr(os, "getuid"):
        pytest.skip("ownership checks unavailable")
    actual = os.getuid()
    monkeypatch.setattr(module.os, "getuid", lambda: actual + 1)
    with pytest.raises(PoolStoreError, match="owned"):
        store.acquire([account()], "m", 60)


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_hardlinked_database_or_sidecar_is_rejected_before_chmod(store, tmp_path, suffix):
    target = store.path if not suffix else Path(str(store.path) + suffix)
    if suffix:
        target.write_text("unrelated inode")
    target.chmod(0o644)
    other_name = tmp_path / "other-name"
    os.link(target, other_name)
    content = other_name.read_bytes()
    with pytest.raises(PoolStoreError, match="hardlinked"):
        PoolStore(store.path)
    assert other_name.read_bytes() == content
    assert stat.S_IMODE(other_name.stat().st_mode) == 0o644


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf"), True])
def test_invalid_ttl_does_not_admit(store, ttl):
    with pytest.raises(ValueError):
        store.acquire([account()], "m", ttl)
    assert store.status([account()])["in_flight"] == 0


def test_invalid_configuration_and_failure_roll_back_without_losing_lease(store):
    for accounts in ([account(), account()], [account(cap=0)], [account(priority=True)]):
        with pytest.raises(ValueError):
            store.acquire(accounts, "m", 60)
    lease = store.acquire([account()], "m", 60)
    with pytest.raises(ValueError):
        store.finish(lease, failure(retry_at=float("nan")))
    assert store.status([account()])["in_flight"] == 1
    store.finish(lease)
    assert store.status([account()])["in_flight"] == 0
