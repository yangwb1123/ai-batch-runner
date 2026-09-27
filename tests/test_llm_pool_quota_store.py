"""Quota cache migration, shared refresh admission and observation boundaries."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import llm_pool_quota_store as quota
from pbatch import llm_pool_store as module
from pbatch.llm_pool_store import PoolAccount, PoolStore, PoolStoreError, UsageRefresh


# Frozen v1 DDL: migration tests must not silently follow the current schema.
V1_SCHEMA = (
    "CREATE TABLE controls (account_id TEXT PRIMARY KEY, "
    "enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), "
    "auth_failed INTEGER NOT NULL CHECK(auth_failed IN (0,1)))",
    "CREATE TABLE leases (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, "
    "quota_group TEXT NOT NULL, model TEXT NOT NULL, expires_at REAL NOT NULL, "
    "cap INTEGER NOT NULL CHECK(cap>0), probe INTEGER NOT NULL CHECK(probe IN (0,1)))",
    "CREATE TABLE blocks (quota_group TEXT NOT NULL, model TEXT NOT NULL, "
    "window TEXT NOT NULL, retry_at REAL NOT NULL, reset_known INTEGER NOT NULL "
    "CHECK(reset_known IN (0,1)), version TEXT NOT NULL, "
    "PRIMARY KEY(quota_group,model,window))",
    "CREATE TABLE probes (lease_id TEXT NOT NULL REFERENCES leases(id) ON DELETE CASCADE, "
    "version TEXT NOT NULL, PRIMARY KEY(lease_id,version))",
    "CREATE TABLE events (id INTEGER PRIMARY KEY, at REAL NOT NULL, "
    "kind TEXT NOT NULL, account_id TEXT NOT NULL, quota_group TEXT NOT NULL, "
    "model TEXT NOT NULL)",
)


def account(name="a", group="g", **kwargs):
    return PoolAccount(name, "/private/NEVER_READ_CREDENTIAL", group, **kwargs)


def windows(reset=2000.0, limited=""):
    return {name: {"status": "rate-limited" if name == limited else "ok",
                   "percent": 100 if name == limited else 20, "resets_at": reset + index * 1000}
            for index, name in enumerate(("5h", "weekly", "monthly"))}


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: value[0])
    return value


@pytest.fixture
def store(tmp_path):
    return PoolStore(tmp_path / "state" / "pool.sqlite")


def observe(store, payload=None, **kwargs):
    ticket = store.begin_usage_refresh(account(), 30)
    assert ticket is not None
    store.finish_usage_refresh(ticket, windows() if payload is None else payload, **kwargs)
    return ticket


def usage(store):
    return store.status([account()], "m")["accounts"][0]["usage"]


def snapshot(path):
    with sqlite3.connect(path) as connection:
        return tuple(connection.iterdump())


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        for statement in V1_SCHEMA:
            connection.execute(statement)
        connection.execute("PRAGMA application_id=1280331596")
        connection.execute("PRAGMA user_version=1")
        connection.executemany("INSERT INTO controls VALUES(?,?,?)", [("paused", 0, 0), ("auth", 1, 1)])
        connection.executemany("INSERT INTO leases VALUES(?,?,?,?,?,?,?)", [
            ("live", "a", "g", "m", 2000, 2, 1), ("expired", "a", "g", "m", 999, 2, 0)])
        connection.execute("INSERT INTO blocks VALUES('g','m','weekly',5000,0,'version')")
        connection.execute("INSERT INTO probes VALUES('live','version')")
        connection.execute("INSERT INTO events VALUES(7,900,'acquire','a','g','m')")
    return path


def test_v1_migration_preserves_all_rows_and_schema_including_expired_leases(legacy, clock):
    before = snapshot(legacy)
    store = PoolStore(legacy)
    after = snapshot(legacy)
    for line in before:
        assert line in after
    with sqlite3.connect(legacy) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA application_id").fetchone()[0] == 0x4C504F4C
        assert connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 2
    assert store.status([account()], "m")["in_flight"] == 1
    assert snapshot(legacy) == after
    assert PoolStore(legacy).events()[0]["id"] == 7


@pytest.mark.parametrize("sql", ["PRAGMA user_version=3", "PRAGMA application_id=1",
                                 "DROP TABLE blocks", "ALTER TABLE controls ADD COLUMN extra TEXT",
                                 "CREATE TABLE usage_cache (bad TEXT)"])
def test_wrong_v1_schema_is_rejected_without_migration(legacy, sql):
    with sqlite3.connect(legacy) as connection:
        connection.execute(sql)
    before = snapshot(legacy)
    with pytest.raises(PoolStoreError):
        PoolStore(legacy)
    assert snapshot(legacy) == before


def test_migration_ddl_failure_rolls_back_version_and_every_new_table(legacy, monkeypatch):
    before = snapshot(legacy)
    monkeypatch.setattr(quota, "SCHEMA", (quota.SCHEMA[0], V1_SCHEMA[0]))
    with pytest.raises(PoolStoreError):
        PoolStore(legacy)
    assert snapshot(legacy) == before
    with sqlite3.connect(legacy) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_v2_missing_usage_table_fails_closed(store):
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE usage_refreshes")
    with pytest.raises(PoolStoreError):
        store.status([account()])


def test_unknown_usage_and_successful_group_cache_control_admission(store, clock):
    unknown = usage(store)
    assert unknown == {"state": "unknown", "observed_at": None, "checked_at": None, "fresh_until": None,
                       "next_refresh_at": None, "error": "", "windows": {}}
    assert store.acquire([account()], "m", 60, require_usage=True) is None
    assert store.status([account()], "m", True)["accounts"][0]["state"] == "usage_unknown"
    lease = store.acquire([account()], "m", 60)
    store.finish(lease)
    ticket = observe(store)
    with pytest.raises(FrozenInstanceError):
        ticket.id = "mutated"
    observed = usage(store)
    assert observed == {"state": "fresh", "observed_at": 1000, "checked_at": 1000, "fresh_until": 1060,
                        "next_refresh_at": 1060, "error": "", "windows": windows()}
    sibling = replace(account("b"), max_concurrent=1)
    assert PoolStore(store.path).status([sibling])["accounts"][0]["usage"] == observed
    assert store.begin_usage_refresh(sibling, 30) is None
    assert store.acquire([sibling], "m", 60, require_usage=True).account_id == "b"


@pytest.mark.parametrize("earliest", ["5h", "weekly", "monthly"])
def test_freshness_never_crosses_any_window_reset(store, clock, earliest):
    payload = windows()
    payload[earliest]["resets_at"] = 1010
    observe(store, payload, max_age=900)
    assert usage(store)["fresh_until"] == usage(store)["next_refresh_at"] == 1010
    clock[0] = 1010
    assert usage(store)["state"] == "stale"
    assert store.acquire([account()], "m", 60, require_usage=True) is None
    assert store.begin_usage_refresh(account("b"), 30)


def test_past_reset_observation_is_immediately_stale(store, clock):
    observe(store, windows(reset=999))
    assert usage(store)["state"] == "stale"
    assert store.acquire([account()], "m", 60, require_usage=True) is None
    assert store.begin_usage_refresh(account(), 30)


@pytest.mark.parametrize("require_usage", [False, True])
def test_known_full_stale_cache_stays_blocked_until_reset(store, clock, require_usage):
    observe(store, windows(limited="weekly"), max_age=10)
    clock[0] = 1020
    status = store.status([account(), account("b")], "any-model", require_usage)
    assert all(row["state"] == "blocked" for row in status["accounts"])
    assert status["next_available_at"] == 3000
    assert store.acquire([account()], "any-model", 60, require_usage=require_usage) is None
    assert store.acquire([account("independent", "other")], "m", 60)
    clock[0] = 3000
    assert store.status([account()], "m", require_usage)["accounts"][0]["state"] == (
        "usage_unknown" if require_usage else "available")


@pytest.mark.parametrize("error", ["transport", "invalid_response", "auth", "rate", "service"])
def test_failed_refresh_retains_observation_and_uses_only_usage_error(store, clock, error):
    observe(store, windows(limited="weekly"), max_age=10)
    clock[0] = 1010
    ticket = store.begin_usage_refresh(account(), 30)
    store.finish_usage_refresh(ticket, None, error=error, retry_after=15)
    current = usage(PoolStore(store.path))
    assert current["state"] == "error"
    assert current["checked_at"] == 1010 and current["fresh_until"] is None
    assert current["observed_at"] == 1000
    assert current["windows"] == windows(limited="weekly")
    assert current["next_refresh_at"] == 1025 and current["error"] == error
    assert store.begin_usage_refresh(account("b"), 30) is None
    state = store.status([account()], "m", True)["accounts"][0]
    assert state["state"] == "blocked" and not state["auth_failed"]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT observed_at FROM usage_cache").fetchone()[0] == 1000
    clock[0] = 1025
    observe(store)
    assert usage(store)["error"] == ""
    assert usage(store)["observed_at"] == usage(store)["checked_at"] == 1025
    assert store.acquire([account()], "m", 60, require_usage=True)


def test_failure_without_previous_cache_reports_unknown_admission_and_backoff(store, clock):
    ticket = store.begin_usage_refresh(account(), 30)
    store.finish_usage_refresh(ticket, None)
    state = store.status([account()], "m", True)["accounts"][0]
    assert state["state"] == "usage_unknown" and state["next_available_at"] == 1015
    assert state["usage"]["error"] == "invalid_response" and state["usage"]["windows"] == {}
    assert state["usage"]["observed_at"] is None
    assert store.acquire([account()], "m", 30)


def test_stale_and_forged_tickets_cannot_overwrite_new_refresh(store, clock):
    first = store.begin_usage_refresh(account(), 10)
    clock[0] = 1010
    second = store.begin_usage_refresh(account("b"), 30)
    before = snapshot(store.path)
    for ticket in (first, replace(second, account_id="a"), replace(second, expires_at=9999)):
        store.finish_usage_refresh(ticket, windows(limited="weekly"))
        assert snapshot(store.path) == before
    store.finish_usage_refresh(second, windows())
    finished = snapshot(store.path)
    store.finish_usage_refresh(first, None, error="auth")
    store.finish_usage_refresh(second, None, error="service")
    assert snapshot(store.path) == finished
    assert usage(store)["state"] == "fresh"


def test_expired_refresh_response_cannot_create_cache_and_slot_is_recoverable(store, clock):
    ticket = store.begin_usage_refresh(account(), 5)
    assert usage(store)["state"] == "refreshing"
    assert store.status([account()], "m", True)["next_available_at"] == 1005
    clock[0] = 1005
    store.finish_usage_refresh(ticket, windows())
    assert usage(store)["state"] == "unknown"
    assert store.begin_usage_refresh(account(), 5).id != ticket.id


def test_refresh_observations_never_clear_concurrent_inference_blocks(store, clock):
    ticket = store.begin_usage_refresh(account(), 30)
    lease = store.acquire([account()], "m", 60)
    problem = SimpleNamespace(kind="quota", retry_at=5000, window="weekly", scope="account")
    store.finish(lease, problem)
    store.finish_usage_refresh(ticket, windows())
    state = store.status([account()], "m", True)["accounts"][0]
    assert state["usage"]["state"] == "fresh" and state["state"] == "blocked"
    assert state["next_available_at"] == 5000
    assert state["blocks"][0]["window"] == "weekly"
    assert store.acquire([account()], "other-model", 60, require_usage=True) is None


def test_refresh_respects_controls_but_usage_auth_does_not_disable_inference(store, clock):
    assert store.begin_usage_refresh(replace(account(), enabled=False), 30) is None
    store.set_enabled("a", False)
    assert store.begin_usage_refresh(account(), 30) is None
    store.set_enabled("a", True)
    store.finish(store.acquire([account()], "m", 30), SimpleNamespace(kind="auth"))
    assert store.begin_usage_refresh(account(), 30) is None
    store.reset_auth("a")
    ticket = store.begin_usage_refresh(account(), 30)
    store.finish_usage_refresh(ticket, None, error="auth")
    assert store.status([account()], "m", True)["accounts"][0]["state"] == "usage_unknown"
    assert store.acquire([account()], "m", 30)


def test_threads_and_different_accounts_share_one_refresh_slot(store):
    barrier = threading.Barrier(12)

    def start(index):
        local = store if index % 2 else PoolStore(store.path)
        barrier.wait(timeout=10)
        return local.begin_usage_refresh(account(str(index)), 60)

    with ThreadPoolExecutor(max_workers=12) as executor:
        tickets = [ticket for ticket in executor.map(start, range(12)) if ticket]
    assert len(tickets) == 1
    assert store.begin_usage_refresh(account("other", "independent"), 60)


def test_subprocess_migration_and_refresh_admission_are_exclusive(legacy):
    script = """
import json, sys
from dataclasses import asdict
from pathlib import Path
from pbatch.llm_pool_store import PoolAccount, PoolStore
print('ready', flush=True)
sys.stdin.readline()
store = PoolStore(Path(sys.argv[1]))
ticket = store.begin_usage_refresh(PoolAccount(sys.argv[2], '/never/read', 'g'), 60)
print(json.dumps(asdict(ticket) if ticket else None), flush=True)
"""
    workers = [subprocess.Popen([sys.executable, "-c", script, str(legacy), str(i)],
                               cwd=Path(__file__).resolve().parents[1], text=True,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE) for i in range(6)]
    records = []
    try:
        for worker in workers:
            assert worker.stdout.readline().strip() == "ready"
        for worker in workers:
            worker.stdin.write("go\n")
            worker.stdin.flush()
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=20)
            assert worker.returncode == 0, stderr
            records.append(json.loads(stdout))
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)
    assert len([record for record in records if record]) == 1
    with sqlite3.connect(legacy) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 2


def test_status_is_readonly_redacted_and_return_values_are_detached(store, clock):
    observe(store)
    clock[0] = 1060
    store.begin_usage_refresh(account(), 1)
    store.acquire([account()], "m", 1)
    clock[0] = 1061
    before = snapshot(store.path)
    status = store.status([account()], "m", True)
    assert status["in_flight"] == 0
    assert status["accounts"][0]["usage"]["state"] == "stale"
    status["accounts"][0]["usage"]["windows"]["5h"]["percent"] = 999
    assert usage(store)["windows"]["5h"]["percent"] == 20
    assert snapshot(store.path) == before
    assert "NEVER_READ_CREDENTIAL" not in json.dumps([usage(store), before, store.events()])


@pytest.mark.parametrize("payload", [{}, {"5h": {}}, {"body": "RAW_PROVIDER_SECRET"}])
def test_unstructured_usage_is_rejected_without_persisting_content(store, payload):
    ticket = store.begin_usage_refresh(account(), 30)
    before = snapshot(store.path)
    with pytest.raises(ValueError):
        store.finish_usage_refresh(ticket, payload)
    assert snapshot(store.path) == before
    assert "RAW_PROVIDER_SECRET" not in "\n".join(snapshot(store.path))


@pytest.mark.parametrize("field,value", [("status", "SECRET"), ("percent", True),
                                         ("status", "rate-limited"), ("percent", 100),
                                         ("percent", -1), ("percent", 101), ("percent", 1.5),
                                         ("resets_at", float("inf")), ("resets_at", float("nan")),
                                         ("resets_at", "SECRET"), ("body", "SECRET")])
def test_window_metadata_validation_is_atomic(store, field, value):
    ticket = store.begin_usage_refresh(account(), 30)
    payload = windows()
    payload["weekly"][field] = value
    before = snapshot(store.path)
    with pytest.raises(ValueError):
        store.finish_usage_refresh(ticket, payload)
    assert snapshot(store.path) == before


@pytest.mark.parametrize("sql", [
    "DELETE FROM usage_windows WHERE window='weekly'",
    "DELETE FROM usage_windows WHERE window IN ('weekly','monthly')",
    "DELETE FROM usage_windows",
    "UPDATE usage_windows SET percent=100 WHERE window='5h'",
    "UPDATE usage_windows SET resets_at=1005 WHERE window='5h'",
    "UPDATE usage_windows SET resets_at=1e999 WHERE window='5h'",
    "UPDATE usage_cache SET fresh_until=1e999",
])
def test_damaged_cache_never_admits_even_when_sqlite_quick_check_passes(store, clock, sql):
    observe(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(sql)
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    before = snapshot(store.path)
    for require_usage in (False, True):
        with pytest.raises(PoolStoreError):
            store.status([account()], "m", require_usage=require_usage)
        with pytest.raises(PoolStoreError):
            store.acquire([account()], "m", 30, require_usage=require_usage)
    assert snapshot(store.path) == before


def test_busy_state_remains_busy_when_usage_is_not_fresh(store, clock):
    lease = store.acquire([account()], "m", 10)
    assert store.status([account()], "m", True)["accounts"][0]["state"] == "busy"
    assert store.acquire([account()], "m", 10, require_usage=True) is None
    clock[0] = lease.expires_at
    status = store.status([account()], "m", True)
    assert status["in_flight"] == 0
    assert status["accounts"][0]["state"] == "usage_unknown"


@pytest.mark.parametrize("kwargs", [{"error": "RAW_PROVIDER_SECRET"}, {"max_age": 0},
                                    {"retry_after": float("nan")}, {"max_age": True}])
def test_invalid_completion_options_do_not_consume_ticket(store, kwargs):
    ticket = store.begin_usage_refresh(account(), 30)
    before = snapshot(store.path)
    with pytest.raises(ValueError):
        store.finish_usage_refresh(ticket, None, **kwargs)
    assert snapshot(store.path) == before


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True])
def test_invalid_refresh_ttl_does_not_create_ticket(store, ttl):
    before = snapshot(store.path)
    with pytest.raises(ValueError):
        store.begin_usage_refresh(account(), ttl)
    assert snapshot(store.path) == before
