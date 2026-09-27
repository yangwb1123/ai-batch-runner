"""Account edits versus live gateway snapshots, using only temporary fake keys."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time

import pytest
import yaml

from pbatch.llm_pool_cli import pool_main
from pbatch.llm_pool_config import load_config
from pbatch.llm_pool_gateway import PoolGateway
from pbatch.llm_pool_go import PoolFailure
from pbatch.llm_pool_store import PoolStore
from test_llm_pool_gateway import (
    GatewayClient, KEYS, MODELS, OK, TOKEN, Reply, private_file, serving, upstream,
)


pytestmark = pytest.mark.scenario
CHANGES = {
    "disable": ("update", "account-a", "--disable"),
    "enable": ("update", "account-a", "--enable"),
    "remove": ("remove", "account-a", "--confirm", "account-a"),
    "priority": ("update", "account-a", "--priority", "20"),
    "concurrency": ("update", "account-a", "--max-concurrent", "1"),
}


@pytest.fixture
def pool_file(tmp_path, monkeypatch):
    """Load gateway and CLI from one YAML; every secret reference is relative."""
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(runtime))
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700)
    keys = directory / "keys"
    keys.mkdir(mode=0o700)
    private_file(keys, "token", TOKEN)
    for name, value in zip(("a", "b"), KEYS):
        private_file(keys, name, value)
    document = {
        "schema_version": 1, "pool_id": "account-runtime", "models": MODELS,
        "gateway": {"token_file": "keys/token", "request_timeout": 15},
        "usage": {"enabled": False},
        "accounts": [
            {"id": "account-a", "credential_file": "keys/a", "quota_group": "group-a",
             "priority": 0, "max_concurrent": 2},
            {"id": "account-b", "credential_file": "keys/b", "quota_group": "group-b",
             "priority": 10, "max_concurrent": 1},
        ],
    }
    path = directory / "pool.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)
    settings = load_config(str(path))
    assert settings.state_file == runtime / "llm-pools" / document["pool_id"] / "state.sqlite3"
    assert settings.token_file == keys / "token"
    assert [item.credential_file for item in settings.accounts] == [str(keys / "a"), str(keys / "b")]
    return path


@contextmanager
def gateway(settings, upstream):
    """Reuse the existing fake provider/client, but keep the YAML-derived state path."""
    with serving(PoolGateway(settings, port=0, upstream_url=upstream.url)) as server:
        client = GatewayClient(server)
        try:
            yield client
        finally:
            client.idle()


def invoke(path, capsys, *arguments):
    assert pool_main(["--config", str(path), *arguments]) == 0
    output = capsys.readouterr()
    assert not output.err
    assert all(secret not in output.out for secret in (TOKEN, *KEYS))
    return json.loads(output.out)


def change(path, action, capsys):
    report = invoke(path, capsys, "account", *CHANGES[action])
    assert report["changed"] and report["restart_required"]
    assert report["credentials_changed"] is False
    assert report["runtime_state_changed"] is False
    return load_config(str(path))


def request_from(client, upstream, account_id, key):
    upstream.replies.put(Reply())
    result = client.request()
    assert result.status == 200 and result.body == OK
    assert result.headers["x-pbatch-account"] == account_id
    assert upstream.calls[-1].headers["authorization"] == "Bearer " + key
    client.idle()


@pytest.mark.parametrize("action", ["disable", "remove", "priority"])
def test_config_edit_keeps_live_snapshot_until_gateway_restart(pool_file, upstream, capsys, action):
    original = load_config(str(pool_file))
    with gateway(original, upstream) as live:
        request_from(live, upstream, "account-a", KEYS[0])
        updated = change(pool_file, action, capsys)
        assert updated != original and updated.state_file == original.state_file
        assert live.server.settings == original
        assert live.server.settings.accounts[0].enabled
        assert live.server.keys["account-a"] == KEYS[0]
        request_from(live, upstream, "account-a", KEYS[0])
    with gateway(updated, upstream) as restarted:
        assert restarted.server.settings == updated
        if action in ("disable", "remove"):
            assert "account-a" not in restarted.server.keys
        request_from(restarted, upstream, "account-b", KEYS[1])
    assert len(upstream.calls) == 3


@pytest.mark.parametrize("action", ["disable", "remove"])
def test_pause_before_edit_stops_new_admission_without_aborting_inflight(
        pool_file, upstream, capsys, action):
    original = load_config(str(pool_file))
    release = threading.Event()
    held = Reply(hold=release)
    with gateway(original, upstream) as live, ThreadPoolExecutor(max_workers=1) as workers:
        upstream.replies.put(held)
        pending = workers.submit(live.request)
        try:
            assert held.entered.wait(3), "fake provider did not receive the in-flight request"
            store = live.server.store
            assert store.status(original.accounts)["accounts"][0]["in_flight"] == 1
            # A has two slots, so a refusal here must be pause, not saturation.
            invoke(pool_file, capsys, "pause", "account-a")
            assert store.acquire(original.accounts, "chat-model", 30, exclude={"group-b"}) is None
            updated = change(pool_file, action, capsys)
            assert not pending.done() and not held.sent.is_set()
            upstream.replies.put(Reply())
            second = live.request()
            assert second.status == 200 and second.headers["x-pbatch-account"] == "account-b"
            assert upstream.calls[-1].headers["authorization"] == "Bearer " + KEYS[1]
            row = store.status(original.accounts)["accounts"][0]
            assert row["state"] == "disabled" and row["in_flight"] == 1
            assert not pending.done() and not held.sent.is_set()
        finally:
            release.set()
        completed = pending.result(timeout=5)
        assert completed.status == 200 and completed.body == OK
        assert completed.headers["x-pbatch-account"] == "account-a"
        assert held.sent.wait(1), "fake provider did not finish the original response"
        live.idle()
    with gateway(updated, upstream) as restarted:
        assert restarted.server.store.status(original.accounts)["accounts"][0]["enabled"] is False
        request_from(restarted, upstream, "account-b", KEYS[1])
    assert [call.headers["authorization"] for call in upstream.calls] == [
        "Bearer " + KEYS[0], "Bearer " + KEYS[1], "Bearer " + KEYS[1],
    ]


def seed_history(settings):
    """Pause, auth failure, inference cooldown and usage cache are independent."""
    store = PoolStore(settings.state_file)
    account = settings.accounts[0]
    first = store.acquire(settings.accounts, "chat-model", 60)
    second = store.acquire(settings.accounts, "chat-model", 60)
    assert first.account_id == second.account_id == account.id
    ticket = store.begin_usage_refresh(account, 30)
    assert ticket is not None
    resets_at = time.time() + 3600
    windows = {name: {"status": "ok", "percent": 10, "resets_at": resets_at}
               for name in ("5h", "weekly", "monthly")}
    windows["monthly"].update(status="rate-limited", percent=100)
    store.finish_usage_refresh(ticket, windows, max_age=3600)
    store.finish(first, PoolFailure("auth", "authentication_failed"))
    store.finish(second, PoolFailure("quota", "GoUsageLimitError", resets_at, "5h"))
    store.set_enabled(account.id, False)
    row = store.status(settings.accounts)["accounts"][0]
    assert row["enabled"] is False and row["auth_failed"] is True
    assert row["blocks"] and row["usage"]["windows"] == windows
    return store, row


def file_snapshot(settings):
    # These are exclusively test-created keys and a quiescent temporary database.
    paths = [settings.token_file, settings.state_file,
             *(Path(account.credential_file) for account in settings.accounts)]
    return {path: (path.stat().st_ino, path.read_bytes()) for path in paths}


@pytest.mark.parametrize("action", ["disable", "enable", "remove", "priority", "concurrency"])
def test_account_edits_and_restart_preserve_keys_auth_and_quota_history(
        pool_file, upstream, capsys, action):
    original = load_config(str(pool_file))
    if action == "enable":
        change(pool_file, "disable", capsys)
    store, before = seed_history(original)
    events = store.events()
    files = file_snapshot(original)
    updated = change(pool_file, action, capsys)
    assert updated.state_file == original.state_file
    assert file_snapshot(original) == files
    assert store.status(original.accounts)["accounts"][0] == before
    assert store.events() == events
    with gateway(updated, upstream) as restarted:
        assert file_snapshot(original) == files
        # Use the original identity to inspect retained history even after removal.
        assert restarted.server.store.status(original.accounts)["accounts"][0] == before
        assert restarted.server.store.events() == events
        request_from(restarted, upstream, "account-b", KEYS[1])
        assert restarted.server.store.status(original.accounts)["accounts"][0] == before
    for path, saved in files.items():
        if path != original.state_file:
            assert (path.stat().st_ino, path.read_bytes()) == saved


def test_lower_concurrency_applies_after_restart_without_erasing_live_leases(
        pool_file, upstream, capsys):
    original = load_config(str(pool_file))
    with gateway(original, upstream) as live:
        updated = change(pool_file, "concurrency", capsys)
        store, leases = live.server.store, []
        try:
            for _ in range(2):
                lease = store.acquire(original.accounts, "chat-model", 60, exclude={"group-b"})
                assert lease is not None and lease.account_id == "account-a"
                leases.append(lease)
            assert store.status(updated.accounts)["accounts"][0]["in_flight"] == 2
            assert store.acquire(updated.accounts, "chat-model", 60, exclude={"group-b"}) is None
        finally:
            for lease in leases:
                store.finish(lease)
    with gateway(updated, upstream) as restarted:
        store = restarted.server.store
        lease = store.acquire(updated.accounts, "chat-model", 60, exclude={"group-b"})
        assert lease is not None and lease.account_id == "account-a"
        try:
            assert store.acquire(updated.accounts, "chat-model", 60, exclude={"group-b"}) is None
        finally:
            store.finish(lease)
    assert upstream.calls == []


def test_readding_same_identity_and_enabling_does_not_resume_or_clear_failures(
        pool_file, upstream, capsys):
    original = load_config(str(pool_file))
    _, before = seed_history(original)
    change(pool_file, "remove", capsys)
    invoke(pool_file, capsys, "account", "add", "account-a", "--quota-group", "group-a",
           "--credential-file", "keys/a", "--max-concurrent", "2", "--disabled")
    updated = change(pool_file, "enable", capsys)
    assert next(item for item in updated.accounts if item.id == "account-a") == original.accounts[0]
    with gateway(updated, upstream) as restarted:
        store = restarted.server.store
        assert store.status(original.accounts)["accounts"][0] == before
        invoke(pool_file, capsys, "resume", "account-a")
        assert store.status(updated.accounts)["accounts"][0]["state"] == "auth_failed"
        assert store.acquire(updated.accounts, "chat-model", 30, exclude={"group-b"}) is None
        invoke(pool_file, capsys, "reset-auth", "account-a")
        row = store.status(updated.accounts)["accounts"][0]
        assert row["state"] == "blocked" and row["auth_failed"] is False
        assert row["blocks"] == before["blocks"] and row["usage"] == before["usage"]
        assert store.acquire(updated.accounts, "chat-model", 30, exclude={"group-b"}) is None
        request_from(restarted, upstream, "account-b", KEYS[1])
