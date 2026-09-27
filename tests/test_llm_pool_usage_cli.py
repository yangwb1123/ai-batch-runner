"""Operator quota refresh is explicit, bounded, redacted and respects account controls."""

from dataclasses import replace
import json
from pathlib import Path
import time

import pytest

from pbatch import llm_pool_cli as cli, llm_pool_usage_refresh as refresh
from pbatch.llm_pool_config import PoolConfig
from pbatch.llm_pool_secrets import write_secret
from pbatch.llm_pool_store import PoolAccount, PoolStore
from pbatch.llm_pool_usage import UsageQueryError


def windows(percent=20):
    return {name: {"status": "rate-limited" if percent == 100 else "ok",
                   "percent": percent, "resets_at": time.time() + 600}
            for name in ("5h", "weekly", "monthly")}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    accounts = tuple(PoolAccount(name, str(tmp_path / "keys" / name), "group-" + name)
                     for name in ("a", "b"))
    for account in accounts:
        write_secret(Path(account.credential_file), "fake-key-for-testing-" + account.id)
    config = PoolConfig("test", accounts, {"model": "chat/completions"},
                        tmp_path / "keys" / "not-created", tmp_path / "state" / "pool.sqlite3")
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    return config


def test_cached_usage_never_reads_credentials_or_queries(settings, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("cached command must not access secrets or network")
    monkeypatch.setattr(refresh, "read_secret", forbidden)
    monkeypatch.setattr(refresh, "fetch_usage", forbidden)
    assert cli.pool_main(["usage", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["source"] == "cache" and report["advisory"] is True
    assert report["refreshed_groups"] == 0
    assert all(row["usage"]["state"] == "unknown" for row in report["accounts"])


def test_explicit_refresh_then_cache_deduplicates_supplier_requests(settings, monkeypatch, capsys):
    calls = []
    def fetch(key, timeout, **kwargs):
        calls.append((key, timeout))
        return windows()
    monkeypatch.setattr(refresh, "fetch_usage", fetch)
    for expected in (2, 0):
        assert cli.pool_main(["usage", "--refresh", "--json"]) == 0
        text = capsys.readouterr().out
        report = json.loads(text)
        assert report["refreshed_groups"] == expected
        assert all(row["usage"]["state"] == "fresh" for row in report["accounts"])
        assert "fake-key" not in text and "credential_file" not in text
    assert len(calls) == 2
    assert all(timeout == settings.usage_timeout for _, timeout in calls)
    assert not settings.token_file.exists(), "CLI quota reads do not need a gateway token"


def test_partial_refresh_error_is_visible_and_does_not_poison_inference_auth(
        settings, monkeypatch, capsys):
    def fetch(key, timeout, **kwargs):
        if key.endswith("a"):
            raise UsageQueryError("auth")
        return windows()
    monkeypatch.setattr(refresh, "fetch_usage", fetch)
    assert cli.pool_main(["usage", "--refresh"]) == 1
    rows = json.loads(capsys.readouterr().out)["accounts"]
    assert rows[0]["usage"]["state"] == "error" and rows[0]["usage"]["error"] == "auth"
    assert rows[1]["usage"]["state"] == "fresh"
    status = PoolStore(settings.state_file).status(settings.accounts)
    assert all(not row["auth_failed"] for row in status["accounts"])


def test_refresh_respects_pause_without_skipping_an_enabled_key_in_the_same_group(
        settings, monkeypatch, capsys):
    accounts = tuple(replace(item, quota_group="shared") for item in settings.accounts)
    shared = replace(settings, accounts=accounts)
    monkeypatch.setattr(cli, "load_config", lambda _: shared)
    store = PoolStore(shared.state_file)
    store.set_enabled("a", False)
    keys = []
    def fetch(key, timeout, **kwargs):
        keys.append(key)
        return windows()
    monkeypatch.setattr(refresh, "fetch_usage", fetch)
    assert cli.pool_main(["usage", "--refresh"]) == 0
    assert len(keys) == 1 and keys[0].endswith("b")
    assert json.loads(capsys.readouterr().out)["refreshed_groups"] == 1
    assert store.status(accounts)["accounts"][0]["state"] == "disabled"


def test_filter_does_not_read_other_key_and_unknown_account_does_not_create_state(
        settings, monkeypatch, capsys):
    def fetch(key, timeout, **kwargs):
        assert key.endswith("b")
        return windows(100)
    monkeypatch.setattr(refresh, "fetch_usage", fetch)
    assert cli.pool_main(["usage", "--account", "unknown", "--refresh"]) == 2
    assert not settings.state_file.exists()
    capsys.readouterr()
    assert cli.pool_main(["usage", "--account", "b", "--refresh"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in report["accounts"]] == ["b"]
    assert report["accounts"][0]["usage"]["windows"]["5h"]["percent"] == 100
    store = PoolStore(settings.state_file)
    assert store.acquire((settings.accounts[1],), "model", 10) is None


def test_status_honors_enabled_preflight_without_reading_secrets(settings, monkeypatch, capsys):
    enabled = replace(settings, usage_enabled=True)
    monkeypatch.setattr(cli, "load_config", lambda _: enabled)
    assert cli.pool_main(["status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "unavailable"
    assert all(row["state"] == "usage_unknown" for row in report["accounts"])
