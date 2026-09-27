"""Normalized quota observations and fenced refreshes inside PoolStore transactions.

Only the three public usage windows are stored. This module neither fetches usage
nor reads credentials. Inference cooldowns and authentication controls are never
updated by a usage observation, including an unsuccessful usage authentication.
"""

from __future__ import annotations

import math
import sqlite3
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class UsageRefresh:
    id: str
    account_id: str
    quota_group: str
    expires_at: float


ERRORS = frozenset({"transport", "invalid_response", "auth", "rate", "service"})
WINDOWS = ("5h", "weekly", "monthly")
SCHEMA = (
    "CREATE TABLE usage_cache (quota_group TEXT PRIMARY KEY, observed_at REAL, "
    "checked_at REAL NOT NULL, fresh_until REAL, next_refresh_at REAL NOT NULL, "
    "error TEXT NOT NULL CHECK(error IN ('','transport','invalid_response','auth','rate','service')))",
    "CREATE TABLE usage_windows (quota_group TEXT NOT NULL REFERENCES usage_cache(quota_group) "
    "ON DELETE CASCADE, window TEXT NOT NULL CHECK(window IN ('5h','weekly','monthly')), "
    "status TEXT NOT NULL CHECK(status IN ('ok','rate-limited')), percent INTEGER NOT NULL "
    "CHECK(typeof(percent)='integer' AND percent BETWEEN 0 AND 100), resets_at REAL NOT NULL, "
    "PRIMARY KEY(quota_group,window))",
    "CREATE TABLE usage_refreshes (quota_group TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, "
    "account_id TEXT NOT NULL, expires_at REAL NOT NULL)",
)


def _number(value, *, positive=False):
    if type(value) not in (int, float):
        raise ValueError("usage timing must be finite numeric metadata")
    try:
        valid = math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or (positive and value <= 0):
        raise ValueError("usage timing must be finite and durations positive")
    return float(value)


def _windows(value):
    if not isinstance(value, dict) or set(value) != set(WINDOWS):
        raise ValueError("usage requires exactly the three supported windows")
    normalized = {}
    for window in WINDOWS:
        item = value[window]
        if not isinstance(item, dict) or set(item) != {"status", "percent", "resets_at"}:
            raise ValueError("usage windows require only status, percent and resets_at")
        if item["status"] not in ("ok", "rate-limited"):
            raise ValueError("invalid usage window status")
        if type(item["percent"]) is not int or not 0 <= item["percent"] <= 100:
            raise ValueError("usage percent must be an integer between 0 and 100")
        if (item["status"] == "rate-limited") != (item["percent"] == 100):
            raise ValueError("usage status and percent are inconsistent")
        normalized[window] = {"status": item["status"], "percent": item["percent"],
                              "resets_at": _number(item["resets_at"])}
    return normalized


def begin_refresh(connection, account, ttl, now):
    """Reserve only the usage refresh slot; an inference lease is independent."""
    expires_at = _number(now + _number(ttl, positive=True))
    control = connection.execute("SELECT enabled,auth_failed FROM controls "
                                 "WHERE account_id=?", (account.id,)).fetchone()
    if not account.enabled or (control and (not control["enabled"] or control["auth_failed"])):
        return None
    cache = connection.execute("SELECT next_refresh_at FROM usage_cache WHERE quota_group=?",
                               (account.quota_group,)).fetchone()
    if cache and cache["next_refresh_at"] > now:
        return None
    active = connection.execute("SELECT expires_at FROM usage_refreshes WHERE quota_group=?",
                                (account.quota_group,)).fetchone()
    if active and active["expires_at"] > now:
        return None
    ticket = UsageRefresh(uuid.uuid4().hex, account.id, account.quota_group, expires_at)
    connection.execute("INSERT INTO usage_refreshes VALUES(?,?,?,?) ON CONFLICT(quota_group) "
                       "DO UPDATE SET id=excluded.id,account_id=excluded.account_id,"
                       "expires_at=excluded.expires_at",
                       (ticket.quota_group, ticket.id, ticket.account_id, ticket.expires_at))
    return ticket


def _current_ticket(connection, ticket, now):
    row = connection.execute("SELECT id,account_id,expires_at FROM usage_refreshes "
                             "WHERE quota_group=?", (ticket.quota_group,)).fetchone()
    if row is None or row["expires_at"] <= now:
        return False
    return (row["id"], row["account_id"], row["expires_at"]) == (
        ticket.id, ticket.account_id, ticket.expires_at)


def finish_refresh(connection, ticket, windows, now, *, max_age, retry_after, error):
    """Return whether a current ticket committed; stale completions are no-ops."""
    if not _current_ticket(connection, ticket, now):
        return False
    if not isinstance(error, str) or error not in ERRORS | {""}:
        raise ValueError("usage error must be a fixed public classification")
    max_age, retry_after = _number(max_age, positive=True), _number(retry_after, positive=True)
    if windows is None:
        _failed(connection, ticket.quota_group, now, retry_after, error or "invalid_response")
    else:
        if error:
            raise ValueError("a successful usage observation cannot include an error")
        _observed(connection, ticket.quota_group, _windows(windows), now, max_age)
    connection.execute("DELETE FROM usage_refreshes WHERE quota_group=? AND id=?",
                       (ticket.quota_group, ticket.id))
    return True


def _failed(connection, group, now, retry_after, error):
    connection.execute("INSERT INTO usage_cache VALUES(?,NULL,?,NULL,?,?) "
                       "ON CONFLICT(quota_group) DO UPDATE SET checked_at=excluded.checked_at, "
                       "fresh_until=NULL,next_refresh_at=excluded.next_refresh_at,error=excluded.error",
                       (group, now, _number(now + retry_after), error))


def _observed(connection, group, windows, now, max_age):
    fresh_until = min([_number(now + max_age)] + [item["resets_at"] for item in windows.values()])
    connection.execute("INSERT INTO usage_cache VALUES(?,?,?,?,?,'') ON CONFLICT(quota_group) "
                       "DO UPDATE SET observed_at=excluded.observed_at,checked_at=excluded.checked_at, "
                       "fresh_until=excluded.fresh_until,next_refresh_at=excluded.next_refresh_at,error=''",
                       (group, now, now, fresh_until, fresh_until))
    connection.execute("DELETE FROM usage_windows WHERE quota_group=?", (group,))
    connection.executemany("INSERT INTO usage_windows VALUES(?,?,?,?,?)", [
        (group, name, item["status"], item["percent"], item["resets_at"])
        for name, item in windows.items()])


def _cached_windows(connection, group, cache):
    windows = {row["window"]: {"status": row["status"], "percent": row["percent"],
                              "resets_at": row["resets_at"]} for row in connection.execute(
        "SELECT window,status,percent,resets_at FROM usage_windows WHERE quota_group=? "
        "ORDER BY window", (group,))}
    try:
        if cache is not None:
            _number(cache["checked_at"])
            _number(cache["next_refresh_at"])
            if cache["observed_at"] is not None:
                _number(cache["observed_at"])
                windows = _windows(windows)
            elif windows:
                raise ValueError("usage windows lack observation metadata")
            if cache["fresh_until"] is not None:
                fresh_until = _number(cache["fresh_until"])
                if not windows or fresh_until > min(item["resets_at"] for item in windows.values()):
                    raise ValueError("usage freshness exceeds validated windows")
    except ValueError:
        raise sqlite3.DatabaseError("invalid cached usage metadata") from None
    return windows


def usage_status(connection, group, now):
    """Pure projection: observing expiry never writes cache, tickets or events."""
    cache = connection.execute("SELECT observed_at,checked_at,fresh_until,next_refresh_at,error "
                               "FROM usage_cache WHERE quota_group=?", (group,)).fetchone()
    ticket = connection.execute("SELECT expires_at FROM usage_refreshes WHERE quota_group=? "
                                "AND expires_at>?", (group, now)).fetchone()
    windows = _cached_windows(connection, group, cache)
    result = {"state": "unknown", "observed_at": None, "checked_at": None, "fresh_until": None,
              "next_refresh_at": None, "error": "", "windows": windows}
    if cache:
        result.update(dict(cache))
        result["state"] = "stale"
        if cache["fresh_until"] is not None and cache["fresh_until"] > now:
            result["state"] = "fresh"
        if cache["error"]:
            result["state"] = "error"
    if ticket:
        result["state"] = "refreshing"
        result["next_refresh_at"] = max(result["next_refresh_at"] or now, ticket["expires_at"])
    return result


def admission(state, usage, require_usage, now):
    """Combine independent constraints without deleting or weakening any block."""
    state["usage"] = usage
    if state["state"] in ("disabled", "auth_failed"):
        return state
    reset = max([now] + [item["resets_at"] for item in usage["windows"].values()
                         if item["status"] == "rate-limited"])
    if reset > now:
        state["state"] = "blocked"
        state["next_available_at"] = max(state["next_available_at"], reset)
    if require_usage and usage["state"] != "fresh":
        if state["state"] in ("available", "probe_due"):
            state["state"] = "usage_unknown"
        state["next_available_at"] = max(state["next_available_at"], usage["next_refresh_at"] or now)
    return state
