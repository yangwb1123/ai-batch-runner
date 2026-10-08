"""Private, transactional account admission; never reads or stores credentials.

Supply the complete pool configuration on each acquire/status call. Lower priority
numbers win, with input order breaking ties. Times are Unix seconds; callers must
give leases the entire absolute request timeout plus a safety margin. Unknown
resets remain blocks until a successful exclusive probe, not merely until a timer.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import llm_pool_quota_store as quota
from .llm_pool_quota_store import UsageRefresh
from .llm_pool_state_guard import state_guard


@dataclass(frozen=True)
class PoolAccount:
    id: str
    credential_file: str
    quota_group: str
    priority: int = 0
    max_concurrent: int = 1
    enabled: bool = True


@dataclass(frozen=True)
class Lease:
    id: str
    account_id: str
    quota_group: str
    model: str
    expires_at: float


class PoolStoreError(RuntimeError):
    """Unsafe, corrupt or incompatible persistent state; admission is refused."""


_APPLICATION_ID = 0x4C504F4C
_SCHEMA = (
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


def _identifier(value, *, empty=False):
    if empty and value == "":
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[\w.:/@+\-]{1,200}", value):
        raise ValueError("pool metadata must be a bounded identifier")
    return value


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("pool timing must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("pool timing must be a finite positive number")
    return float(value)


def _configuration(accounts, model):
    _identifier(model, empty=True)
    ordered, caps, seen = list(accounts), {}, set()
    for account in ordered:
        _identifier(account.id)
        _identifier(account.quota_group)
        if account.id in seen:
            raise ValueError("duplicate pool account id")
        if type(account.priority) is not int or type(account.enabled) is not bool:
            raise ValueError("invalid pool priority or enabled flag")
        if type(account.max_concurrent) is not int or account.max_concurrent < 1:
            raise ValueError("pool concurrency must be a positive integer")
        seen.add(account.id)
        caps[account.quota_group] = min(caps.get(account.quota_group,
                                               account.max_concurrent), account.max_concurrent)
    return sorted(ordered, key=lambda account: account.priority), caps


def _owned(info):
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PoolStoreError("pool state must be owned by the current user")


def _directories(parent, create):
    for directory in reversed((parent, *parent.parents)):
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise PoolStoreError("pool state directory must not be a symlink")
    _owned(parent.lstat())
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent, flags)
    try:
        _owned(os.fstat(descriptor))
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def _private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise PoolStoreError("pool database and sidecars must be regular non-symlink files")
    _owned(info)
    if info.st_nlink != 1:
        raise PoolStoreError("pool database and sidecars must not be hardlinked")
    # Do not open/close another DB descriptor: POSIX close drops *all* this
    # process's fcntl locks on that inode, including other threads' SQLite locks.
    os.chmod(path, 0o600, follow_symlinks=False)
    checked = path.lstat()
    if (checked.st_dev, checked.st_ino, checked.st_nlink) != (info.st_dev, info.st_ino, 1):
        raise PoolStoreError("pool state changed while checking permissions")


def _bootstrap(path):
    """Serialize publication until its temporary hardlink is removed (POSIX)."""
    with state_guard(path.parent):
        if not path.exists():
            _publish_database(path)


def _publish_database(path):
    """Publish a complete schema atomically; an existing empty DB is corruption."""
    descriptor, name = tempfile.mkstemp(prefix=".pool-init-", dir=path.parent)
    os.close(descriptor)
    try:
        connection = sqlite3.connect(name)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA + quota.SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute("PRAGMA user_version=2")
        finally:
            connection.close()
        try:
            os.link(name, path)
        except FileExistsError:
            pass  # A concurrent initializer already published its complete DB.
    finally:
        os.unlink(name)
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_db(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    schema = {row[0] for row in connection.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")}
    expected = _SCHEMA if version == 1 else _SCHEMA + quota.SCHEMA
    if version not in (1, 2) or application != _APPLICATION_ID or schema != set(expected):
        raise PoolStoreError("incompatible pool database schema")
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise PoolStoreError("corrupt pool database")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise PoolStoreError("corrupt pool lease references")
    if version == 1:
        for statement in quota.SCHEMA:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=2")


def _event(connection, kind, account_id="", quota_group="", model=""):
    connection.execute("INSERT INTO events(at,kind,account_id,quota_group,model) "
                       "VALUES(?,?,?,?,?)", (time.time(), kind, account_id, quota_group, model))
    connection.execute("DELETE FROM events WHERE id NOT IN "
                       "(SELECT id FROM events ORDER BY id DESC LIMIT 256)")


def _availability(leases, blocks, cap, now):
    cap = min([cap] + [row["cap"] for row in leases])
    unknown = any(not block["reset_known"] for block in blocks)
    if unknown or any(row["probe"] for row in leases):
        cap = 1
    available_at = max([now] + [block["retry_at"] for block in blocks])
    state = "probe_due" if unknown else "available"
    if len(leases) >= cap:
        expiries = sorted(row["expires_at"] for row in leases)
        available_at = max(available_at, expiries[len(leases) - cap])
        state = "busy"
    if any(block["retry_at"] > now for block in blocks):
        state = "blocked"
    return state, available_at


class PoolStore:
    """Each operation owns a connection and an IMMEDIATE SQLite transaction.

    POSIX private user state: no symlink components or multiply linked DB files.
    Bootstrap, permission checks and transactions share a directory flock so
    cooperating stores cannot replace a journal during its permission check.
    Path checks assume trusted ancestors and no malicious same-user renames;
    sqlite3 opens by pathname, so checks cannot eliminate all pathname TOCTOU.
    """

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().absolute()
        if ".." in self.path.parts:
            raise PoolStoreError("pool state path must not contain parent traversal")
        try:
            _directories(self.path.parent, create=True)
            _bootstrap(self.path)
            with self._transaction(prune=False):
                pass
        except (OSError, sqlite3.Error) as exc:
            raise PoolStoreError("cannot initialize private pool state") from exc

    @contextmanager
    def _transaction(self, *, prune=True):
        try:
            _directories(self.path.parent, create=False)
            with state_guard(self.path.parent) as deadline:
                connection = None
                try:
                    _private_file(self.path)
                    for suffix in ("-journal", "-wal", "-shm"):
                        sidecar = Path(str(self.path) + suffix)
                        try:
                            _private_file(sidecar)
                        except FileNotFoundError:
                            pass
                    connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                        timeout=max(0.0, deadline - time.monotonic()), isolation_level=None)
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("PRAGMA trusted_schema=OFF")
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.execute("BEGIN IMMEDIATE")
                    _validate_db(connection)
                    if prune:
                        connection.execute("DELETE FROM leases WHERE expires_at<=?", (time.time(),))
                    yield connection
                    connection.commit()
                finally:
                    if connection is not None:
                        connection.close()  # Rollback/close before releasing the directory guard.
        except (OSError, sqlite3.Error) as exc:
            raise PoolStoreError("pool state unavailable; admission refused") from exc

    def acquire(self, accounts: tuple[PoolAccount, ...] | list[PoolAccount],
                model: str, ttl: float, exclude: set[str] | None = None,
                require_usage: bool = False) -> Lease | None:
        """Reserve one group slot; exclude contains quota-group IDs, not key IDs."""
        ordered, caps = _configuration(accounts, model)
        ttl = _positive(ttl)
        with self._transaction() as connection:
            now = time.time()
            for account in ordered:
                if exclude and account.quota_group in exclude:
                    continue
                state = self._account_state(connection, account, model, caps, now, require_usage)
                if state["state"] not in ("available", "probe_due"):
                    continue
                expiry = _positive(now + ttl)
                lease = Lease(uuid.uuid4().hex, account.id, account.quota_group, model, expiry)
                probe = state["state"] == "probe_due"
                connection.execute("INSERT INTO leases VALUES(?,?,?,?,?,?,?)",
                                   (*lease.__dict__.values(), caps[account.quota_group], int(probe)))
                if probe:
                    connection.execute("INSERT INTO probes SELECT ?,version FROM blocks "
                                       "WHERE quota_group=? AND model IN ('',?) "
                                       "AND reset_known=0 AND retry_at<=?",
                                       (lease.id, lease.quota_group, model, now))
                _event(connection, "probe" if probe else "acquire", account.id,
                       account.quota_group, model)
                return lease
        return None

    def _account_state(self, connection, account, model, caps, now, require_usage=False):
        control = connection.execute("SELECT enabled,auth_failed FROM controls "
                                     "WHERE account_id=?", (account.id,)).fetchone()
        enabled = account.enabled and (control is None or bool(control["enabled"]))
        auth_failed = control is not None and bool(control["auth_failed"])
        leases = connection.execute("SELECT expires_at,cap,probe FROM leases WHERE "
                                    "quota_group=? AND expires_at>?", (account.quota_group, now)).fetchall()
        blocks = [dict(row) for row in connection.execute(
            "SELECT model,window,retry_at,reset_known FROM blocks WHERE quota_group=? "
            "AND (?='' OR model IN ('',?)) AND (reset_known=0 OR retry_at>?) "
            "ORDER BY model,window", (account.quota_group, model, model, now))]
        for block in blocks:
            block["reset_known"] = bool(block["reset_known"])
        state, available_at = _availability(leases, blocks, caps[account.quota_group], now)
        if auth_failed:
            state, available_at = "auth_failed", None
        if not enabled:
            state, available_at = "disabled", None
        result = {"id": account.id, "quota_group": account.quota_group,
                "priority": account.priority, "enabled": enabled, "auth_failed": auth_failed,
                "state": state, "in_flight": len(leases), "next_available_at": available_at,
                "blocks": blocks}
        usage = quota.usage_status(connection, account.quota_group, now)
        return quota.admission(result, usage, require_usage, now)

    def finish(self, lease: Lease, failure=None, *, probe_after: float = 60) -> None:
        """Idempotently finish a live lease; stale or mismatched completions do nothing."""
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM leases WHERE id=?", (lease.id,)).fetchone()
            if row is None or any(row[key] != value for key, value in lease.__dict__.items()):
                return
            kind = "success" if failure is None else getattr(failure, "kind", "")
            if kind in ("quota", "rate", "rate_limit"):
                self._block(connection, lease, failure, probe_after)
            elif kind == "auth":
                connection.execute("INSERT INTO controls VALUES(?,1,1) ON CONFLICT(account_id) "
                                   "DO UPDATE SET auth_failed=1", (lease.account_id,))
            elif failure is None:
                connection.execute("DELETE FROM blocks WHERE version IN "
                                   "(SELECT version FROM probes WHERE lease_id=?)", (lease.id,))
            elif kind not in ("service", "request"):
                raise ValueError("unrecognized pool failure kind")
            connection.execute("DELETE FROM leases WHERE id=?", (lease.id,))
            _event(connection, kind, lease.account_id, lease.quota_group, lease.model)

    def _block(self, connection, lease, failure, probe_after):
        now = time.time()
        retry_at = getattr(failure, "retry_at", None)
        known = retry_at is not None
        retry_at = _positive(retry_at) if known else now + min(3600, max(1, _positive(probe_after)))
        scope = getattr(failure, "scope", "account")
        if scope not in ("model", "account", "quota_group"):
            raise ValueError("unrecognized pool failure scope")
        model = lease.model if scope == "model" else ""
        window = _identifier(getattr(failure, "window", None) or "unknown")
        old = connection.execute("SELECT retry_at,reset_known FROM blocks WHERE "
                                 "quota_group=? AND model=? AND window=?",
                                 (lease.quota_group, model, window)).fetchone()
        if old and old["retry_at"] > retry_at:
            retry_at, known = old["retry_at"], bool(old["reset_known"])
        connection.execute("INSERT INTO blocks VALUES(?,?,?,?,?,?) ON CONFLICT "
                           "(quota_group,model,window) DO UPDATE SET retry_at=excluded.retry_at, "
                           "reset_known=excluded.reset_known,version=excluded.version",
                           (lease.quota_group, model, window, retry_at, int(known), uuid.uuid4().hex))

    def status(self, accounts, model: str = "", require_usage: bool = False) -> dict:
        """Redacted snapshot; next_available_at is a timer/probe time, not a quota reset.

        Empty model reports all model blocks conservatively. in_flight counts group
        slots (top-level counts each slot once); None means manual action is needed.
        """
        ordered, caps = _configuration(accounts, model)
        with self._transaction(prune=False) as connection:
            now = time.time()
            rows = [self._account_state(connection, account, model, caps, now, require_usage)
                    for account in ordered]
            times = [row["next_available_at"] for row in rows if row["next_available_at"] is not None]
            counts = {row["quota_group"]: row["in_flight"] for row in rows}
            ready = any(row["state"] in ("available", "probe_due") for row in rows)
            return {"accounts": rows, "state": "available" if ready else "unavailable",
                    "in_flight": sum(counts.values()), "next_available_at": min(times, default=None)}

    def begin_usage_refresh(self, account: PoolAccount, ttl: float) -> UsageRefresh | None:
        """Reserve an exclusive group refresh if its cache timer and controls permit."""
        _configuration([account], "")
        with self._transaction(prune=False) as connection:
            ticket = quota.begin_refresh(connection, account, ttl, time.time())
            if ticket:
                _event(connection, "usage_refresh", ticket.account_id, ticket.quota_group)
            return ticket

    def finish_usage_refresh(self, ticket: UsageRefresh, windows: dict | None, *,
                             max_age: float = 60, retry_after: float = 15, error: str = "") -> None:
        """Cache validated metadata; unsuccessful refreshes never change inference auth."""
        with self._transaction(prune=False) as connection:
            changed = quota.finish_refresh(connection, ticket, windows, time.time(),
                                           max_age=max_age, retry_after=retry_after, error=error)
            if changed:
                _event(connection, "usage_error" if windows is None else "usage_observed",
                       ticket.account_id, ticket.quota_group)

    def set_enabled(self, account_id, enabled: bool) -> None:
        """Persist operator pause/resume independently of auth and quota state."""
        _identifier(account_id)
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        with self._transaction() as connection:
            connection.execute("INSERT INTO controls VALUES(?,?,0) ON CONFLICT(account_id) "
                               "DO UPDATE SET enabled=excluded.enabled", (account_id, int(enabled)))
            _event(connection, "resume" if enabled else "pause", account_id)

    def reset_auth(self, account_id) -> None:
        """Explicit credential reset; never resumes a paused account or removes quota blocks."""
        _identifier(account_id)
        with self._transaction() as connection:
            connection.execute("UPDATE controls SET auth_failed=0 WHERE account_id=?", (account_id,))
            _event(connection, "reset_auth", account_id)

    def events(self, limit=256) -> list:
        """Newest metadata-only events first, bounded to the last 256 records."""
        if type(limit) is not int or limit < 0:
            raise ValueError("event limit must be a nonnegative integer")
        with self._transaction(prune=False) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 256),))]
