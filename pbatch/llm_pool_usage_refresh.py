"""Quota preflight and explicit operator refresh, sharing fenced SQLite tickets."""

from __future__ import annotations

from pathlib import Path
import time

from .llm_pool_config import GO_ORIGIN
from .llm_pool_secrets import read_secret
from .llm_pool_usage import UsageQueryError, fetch_usage


def refresh_account(settings, store, account, *, key=None, timeout=None, upstream_url=GO_ORIGIN):
    """Refresh at most once per group/cache interval; never override pause/auth state."""
    timeout = settings.usage_timeout if timeout is None else min(timeout, settings.usage_timeout)
    if timeout <= 0:
        return False
    ticket = store.begin_usage_refresh(account, timeout + 1)
    if ticket is None:
        return False
    windows, error = None, "service"
    try:
        credential = read_secret(Path(account.credential_file)) if key is None else key
        windows = fetch_usage(credential, timeout, upstream_url=upstream_url)
        error = ""
    except UsageQueryError as exc:
        error = exc.code
    except (OSError, ValueError):
        error = "auth"
    finally:
        store.finish_usage_refresh(ticket, windows, max_age=settings.usage_max_age,
                                   retry_after=min(15, settings.usage_max_age), error=error)
    return True


def prepare_usage(settings, store, keys, model, tried, request):
    """Lazily check candidates in priority order within the model request deadline."""
    accounts = {item.id: item for item in settings.accounts}
    rows = store.status(settings.accounts, model, require_usage=True)["accounts"]
    visited = set(tried)
    for row in rows:
        group = row["quota_group"]
        if group in visited or row["state"] in ("disabled", "auth_failed", "busy", "blocked"):
            continue
        visited.add(group)
        if row["state"] in ("available", "probe_due"):
            return
        if row["usage"]["state"] == "fresh":
            continue
        refresh_account(settings, store, accounts[row["id"]], key=keys[row["id"]],
                        timeout=request.remaining(), upstream_url=request.origin.geturl())
        fresh = store.status(settings.accounts, model, require_usage=True)["accounts"]
        candidate = next(item for item in fresh if item["id"] == row["id"])
        if candidate["state"] in ("available", "probe_due"):
            return


def usage_report(settings, store, *, account_id="", refresh=False) -> dict:
    """Only an explicit CLI refresh reads credentials or queries the supplier."""
    accounts = [item for item in settings.accounts if not account_id or item.id == account_id]
    if account_id and not accounts:
        raise ValueError("unknown pool account")
    refreshed, groups = 0, set()
    if refresh:
        permitted = {row["id"] for row in store.status(settings.accounts)["accounts"]
                     if row["enabled"] and not row["auth_failed"]}
        for account in sorted(accounts, key=lambda item: item.priority):
            if account.quota_group not in groups and account.id in permitted:
                if refresh_account(settings, store, account):
                    refreshed += 1
                groups.add(account.quota_group)
    rows = store.status(settings.accounts)["accounts"]
    selected = [{"id": row["id"], "quota_group": row["quota_group"], "usage": row["usage"]}
                for row in rows if not account_id or row["id"] == account_id]
    ok = all(row["usage"]["state"] == "fresh" for row in selected) if refresh else True
    return {"source": "cache", "advisory": True, "generated_at": time.time(),
            "refresh_requested": refresh, "refreshed_groups": refreshed, "ok": ok,
            "accounts": selected}
