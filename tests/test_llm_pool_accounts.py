"""Config-only account lifecycle; test inputs never contain real credentials."""

from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from pbatch import llm_pool_cli as cli
from pbatch.llm_pool_accounts import changed_document, initial_document
from pbatch.llm_pool_config import config_path, load_config


BASE = ["init", "--account", "go-a", "--quota-group", "subscription-a",
        "--model", "glm-5.1=chat/completions"]


def invoke(path, *args):
    return cli.pool_main(["--config", str(path), *args])


def report(capsys):
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def pool(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config" / "pool.yaml"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "never-create-runtime"))
    assert invoke(path, *BASE) == 0
    capsys.readouterr()
    return path


def test_init_is_config_only_and_lists_without_reading_secrets_or_state(tmp_path, monkeypatch, capsys):
    from pbatch import llm_pool_secrets, llm_pool_store
    def forbidden(*args, **kwargs):
        raise AssertionError("management must not access credentials or runtime state")
    monkeypatch.setattr(llm_pool_secrets, "read_secret", forbidden)
    monkeypatch.setattr(llm_pool_secrets, "write_secret", forbidden)
    monkeypatch.setattr(llm_pool_store, "PoolStore", forbidden)
    monkeypatch.setattr(cli, "PoolStore", forbidden)
    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    path = tmp_path / "new" / "pool.yaml"
    assert invoke(path, *BASE) == 0
    result = report(capsys)
    assert result["changed"] and not result["credentials_changed"]
    assert result["changes"]["before"] is None
    settings = load_config(str(path))
    assert settings.usage_enabled and settings.accounts[0].id == "go-a"
    assert not settings.state_file.exists() and not settings.token_file.exists()
    assert not (path.parent / "llm-pool-keys").exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert invoke(path, "account", "list", "--json") == 0
    listing = report(capsys)
    assert listing["configuration_only"] and not listing["credentials_checked"]
    assert listing["revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert listing["accounts"] == [{"id": "go-a", "quota_group": "subscription-a",
                                     "priority": 0, "max_concurrent": 1, "enabled": True}]
    assert "credential_file" not in json.dumps(listing)


def test_init_dry_run_creates_nothing_and_refuses_existing_file(tmp_path, capsys):
    path = tmp_path / "new" / "pool.yaml"
    assert invoke(path, *BASE, "--dry-run") == 0
    preview = report(capsys)
    assert preview["dry_run"] and preview["backup_file"] is None
    assert not path.parent.exists()
    assert invoke(path, *BASE) == 0
    original = path.read_bytes()
    capsys.readouterr()
    for extra in ([], ["--dry-run"]):
        assert invoke(path, *BASE, *extra) == 2
        assert path.read_bytes() == original


@pytest.mark.parametrize("options", [
    ["--pool-id", "../outside"], ["--account", "../outside"], ["--quota-group", ""],
    ["--model", "bad"], ["--model", "glm-5.1=messages"], ["--model", "m=unknown"],
    ["--port", "0"], ["--max-concurrent", "0"], ["--priority", "-1"],
])
def test_invalid_init_never_publishes_config(tmp_path, options, capsys):
    path = tmp_path / "new" / "pool.yaml"
    assert invoke(path, *BASE, *options) == 2
    assert not path.exists()
    output = capsys.readouterr()
    assert "Traceback" not in output.err


def test_add_update_remove_preserve_original_backups_and_runtime_boundary(pool, capsys):
    before = pool.read_bytes()
    assert invoke(pool, "account", "add", "go-b", "--quota-group", "subscription-b",
                  "--priority", "20") == 0
    added = report(capsys)
    assert Path(added["backup_file"]).read_bytes() == before
    assert added["restart_required"] and not added["runtime_state_changed"]
    assert [account.id for account in load_config(str(pool)).accounts] == ["go-a", "go-b"]
    assert invoke(pool, "account", "update", "go-b", "--priority", "5",
                  "--max-concurrent", "2", "--disable") == 0
    updated = report(capsys)
    assert updated["changes"]["before"]["priority"] == 20
    assert updated["changes"]["after"]["priority"] == 5
    account = load_config(str(pool)).accounts[1]
    assert (account.max_concurrent, account.enabled) == (2, False)
    assert invoke(pool, "account", "remove", "go-b", "--confirm", "go-b") == 0
    removed = report(capsys)
    assert removed["changes"]["after"] is None
    assert not removed["credentials_changed"] and not removed["runtime_state_changed"]
    assert [account.id for account in load_config(str(pool)).accounts] == ["go-a"]
    assert Path(added["backup_file"]).read_bytes() == before
    assert not load_config(str(pool)).state_file.exists()


def test_noop_update_preserves_comments_bytes_and_does_not_backup(pool, capsys):
    original = b"# important user notes\n" + pool.read_bytes()
    pool.write_bytes(original)
    before = set(pool.parent.iterdir())
    assert invoke(pool, "account", "update", "go-a", "--priority", "0", "--enable") == 0
    result = report(capsys)
    assert not result["changed"] and not result["restart_required"]
    assert result["backup_file"] is None
    assert pool.read_bytes() == original
    assert set(pool.parent.iterdir()) == before


@pytest.mark.parametrize("args", [
    ["account", "update", "missing", "--priority", "2"],
    ["account", "update", "go-a"], ["account", "update", "go-a", "--max-concurrent", "0"],
    ["account", "add", "go-a", "--quota-group", "other"],
    ["account", "remove", "go-a", "--confirm", "go-a"],
    ["account", "remove", "missing", "--confirm", "missing"],
])
def test_bad_account_changes_preserve_config(pool, args, capsys):
    original = pool.read_bytes()
    assert invoke(pool, *args) == 2
    assert pool.read_bytes() == original
    assert "Traceback" not in capsys.readouterr().err


def test_remove_requires_exact_confirmation_and_dry_run_is_readonly(pool, capsys):
    assert invoke(pool, "account", "add", "go-b", "--quota-group", "b") == 0
    capsys.readouterr()
    original, files = pool.read_bytes(), set(pool.parent.iterdir())
    for confirmation in ([], ["--confirm", "go-a"]):
        assert invoke(pool, "account", "remove", "go-b", *confirmation) == 2
        assert pool.read_bytes() == original
    capsys.readouterr()
    assert invoke(pool, "account", "remove", "go-b", "--dry-run") == 0
    result = report(capsys)
    assert result["changes"]["after"] is None and result["dry_run"]
    assert pool.read_bytes() == original and set(pool.parent.iterdir()) == files


def test_preview_revision_prevents_applying_over_subsequent_changes(pool, capsys):
    assert invoke(pool, "account", "update", "go-a", "--priority", "7", "--dry-run") == 0
    preview = report(capsys)
    assert invoke(pool, "account", "update", "go-a", "--priority", "9") == 0
    capsys.readouterr()
    before = pool.read_bytes()
    assert invoke(pool, "account", "update", "go-a", "--priority", "7",
                  "--if-revision", preview["revision"]) == 2
    assert pool.read_bytes() == before
    capsys.readouterr()
    revision = hashlib.sha256(before).hexdigest()
    assert invoke(pool, "account", "update", "go-a", "--priority", "7",
                  "--if-revision", revision) == 0
    assert load_config(str(pool)).accounts[0].priority == 7


def test_removed_account_key_and_auth_history_are_retained(pool, capsys):
    from pbatch.llm_pool_secrets import write_secret
    from pbatch.llm_pool_store import PoolStore
    from pbatch.llm_pool_go import PoolFailure
    assert invoke(pool, "account", "add", "go-b", "--quota-group", "b") == 0
    settings = load_config(str(pool))
    account = settings.accounts[1]
    key = Path(account.credential_file)
    write_secret(key, "fake-secret-kept-by-removal")
    store = PoolStore(settings.state_file)
    lease = store.acquire([account], "glm-5.1", 30)
    store.finish(lease, PoolFailure("auth", "bad_auth"))
    store.set_enabled("go-b", False)
    state = store.status(settings.accounts)["accounts"][1]
    capsys.readouterr()
    assert invoke(pool, "account", "remove", "go-b", "--confirm", "go-b") == 0
    assert key.read_text().strip() == "fake-secret-kept-by-removal"
    after = store.status(settings.accounts)["accounts"][1]
    assert after["auth_failed"] == state["auth_failed"] is True
    assert after["enabled"] == state["enabled"] is False
    assert "fake-secret" not in capsys.readouterr().out


def test_config_reference_resolution_used_by_new_commands(pool, monkeypatch, capsys):
    from pbatch import config
    monkeypatch.setattr(config, "_section", lambda _: {"config_file": str(pool)})
    assert config_path() == pool
    assert cli.pool_main(["account", "list"]) == 0
    assert report(capsys)["accounts"][0]["id"] == "go-a"


def test_document_transform_does_not_mutate_its_input():
    parser = cli._parser()
    initial = initial_document(parser.parse_args(BASE))
    before = deepcopy(initial)
    result = changed_document(initial, parser.parse_args([
        "account", "update", "go-a", "--priority", "8"]))
    assert initial == before and result["accounts"][0]["priority"] == 8


def test_help_and_invalid_identity_update_do_not_require_a_config(tmp_path):
    root = Path(__file__).resolve().parents[1]
    prefix = [sys.executable, "pi-batch.py", "pool", "--config", str(tmp_path / "absent")]
    for arguments, expected in ((["account", "--help"], 0),
                                (["account", "update", "a", "--quota-group", "new"], 2)):
        result = subprocess.run(prefix + arguments, cwd=root, capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == expected
    assert not (tmp_path / "absent").exists()


def test_busy_config_returns_retryable_exit_code_without_changes(pool, capsys):
    original = pool.read_bytes()
    with (pool.parent / f".{pool.name}.lock").open("r+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert invoke(pool, "account", "update", "go-a", "--priority", "9") == 5
    assert pool.read_bytes() == original
    output = capsys.readouterr()
    assert not output.out and "busy" in output.err


def test_committed_durability_failure_is_never_reported_as_unsaved(pool, monkeypatch, capsys):
    from pbatch import llm_pool_config_io
    original, calls = pool.read_bytes(), []
    fsync = os.fsync
    def fail_after_publication(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError("internal-sensitive-message-not-for-operator")
        return fsync(descriptor)
    monkeypatch.setattr(llm_pool_config_io.os, "fsync", fail_after_publication)
    assert invoke(pool, "account", "update", "go-a", "--priority", "7") == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["committed"] and not result["durability_confirmed"]
    assert result["restart_required"]
    assert Path(result["backup_file"]).read_bytes() == original
    assert load_config(str(pool)).accounts[0].priority == 7
    assert "not-for-operator" not in output.out + output.err
    assert "configuration saved" in output.err


def test_fixed_guidance_does_not_echo_untrusted_account_names(pool, capsys):
    account_id = "do-not-echo-this-sensitive-value"
    assert invoke(pool, "account", "update", account_id, "--priority", "1") == 2
    output = capsys.readouterr()
    assert "unknown pool account" in output.err
    assert account_id not in output.out + output.err
    assert invoke(pool, "account", "remove", "go-a", "--confirm", "go-a") == 2
    assert "last account" in capsys.readouterr().err
