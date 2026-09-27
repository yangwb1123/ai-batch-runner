"""Offline operator diagnostics: aggregate safe errors without touching state."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from pbatch.llm_pool_cli import pool_main
from pbatch.llm_pool_config import load_config
from pbatch.llm_pool_doctor import doctor_report
from pbatch.llm_pool_secrets import write_secret
from test_llm_pool_config import pool_file as unsafe_pool_file


TOKEN = "test-doctor-local-token-0123456789"
KEYS = ("test-doctor-provider-a-0123456789", "test-doctor-provider-b-0123456789")


@pytest.fixture
def pool_file(unsafe_pool_file):
    path, data = unsafe_pool_file
    path.chmod(0o600)
    return path, data


def save(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def credentials(path):
    settings = load_config(str(path))
    write_secret(settings.token_file, TOKEN)
    for account, key in zip(settings.accounts, KEYS):
        write_secret(Path(account.credential_file), key)
    return settings


def check(report, name, account=None):
    return next(item for item in report["checks"]
                if item["check"] == name and item.get("account") == account)


def tree_snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_mode, path.stat().st_mtime_ns,
                                        path.read_bytes() if path.is_file() else None)
            for path in root.rglob("*")}


def test_doctor_aggregates_missing_files_without_creating_anything(pool_file, capsys, monkeypatch):
    path, _ = pool_file
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    before = tree_snapshot(path.parent)
    assert pool_main(["--config", str(path), "doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["read_only"] is True
    assert report["scope"] == "local_setup" and report["schema_version"] == 1
    assert check(report, "configuration")["status"] == "pass"
    assert check(report, "gateway_token")["status"] == "fail"
    assert [check(report, "account_credential", name)["status"]
            for name in ("go-a", "go-b")] == ["fail", "fail"]
    assert check(report, "credential_groups")["status"] == "skip"
    assert "gateway" in report["not_checked"]
    assert tree_snapshot(path.parent) == before


def test_local_success_is_not_provider_or_runtime_attestation(pool_file, capsys, monkeypatch):
    path, _ = pool_file
    settings = credentials(path)
    settings.state_file.parent.mkdir(parents=True)
    settings.state_file.write_bytes(b"not-a-database: doctor must not open this")
    before = tree_snapshot(path.parent)
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    assert pool_main(["--config", str(path), "doctor", "--json"]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["ok"] is True and not output.err
    assert all(item["status"] == "pass" for item in report["checks"])
    assert set(report["not_checked"]) == {"provider_authentication", "provider_quota",
                                         "runtime_state", "gateway", "gateway_account_snapshot",
                                         "gateway_protocol_mapping", "gateway_credential_snapshot",
                                         "gateway_runtime_configuration",
                                         "client_configuration"}
    assert not any(value in output.out for value in (TOKEN, *KEYS, str(settings.token_file)))
    assert not any(hashlib.sha256(key.encode()).hexdigest() in output.out for key in KEYS)
    assert tree_snapshot(path.parent) == before


def test_human_output_has_safe_account_diagnostics_and_scope(pool_file, capsys):
    path, _ = pool_file
    assert pool_main(["--config", str(path), "doctor"]) == 1
    output = capsys.readouterr()
    assert "[FAIL] account_credential / go-a: credential_unavailable" in output.out
    assert "[FAIL] account_credential / go-b: credential_unavailable" in output.out
    assert "供应商认证有效、额度可用" in output.out and "pool key-set" in output.out
    assert not output.err


@pytest.mark.parametrize("kind", ["missing", "yaml", "duplicate", "alias", "oversized",
                                 "symlink", "hardlink", "public-write", "fifo", "directory"])
def test_bad_configuration_is_reported_without_fragments_or_writes(pool_file, capsys, kind):
    path, _ = pool_file
    secret = "must-not-print-credential-123456789"
    if kind in ("missing", "symlink", "fifo", "directory"):
        path.unlink()
    if kind == "yaml":
        path.write_text("accounts: [" + secret)
    elif kind == "duplicate":
        path.write_text("pool_id: a\npool_id: " + secret)
    elif kind == "alias":
        path.write_text("a: &a [*a]")
    elif kind == "oversized":
        path.write_bytes(secret.encode() * 4000)
    elif kind == "symlink":
        path.symlink_to(path.parent / "unknown-target")
    elif kind == "hardlink":
        (path.parent / "second-link").hardlink_to(path)
    elif kind == "public-write":
        path.chmod(0o666)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    assert pool_main(["--config", str(path), "doctor", "--json"]) == 1
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["ok"] is False and report["pool_id"] is None
    assert check(report, "configuration")["code"] == "configuration_unavailable"
    assert secret not in output.out + output.err and "Traceback" not in output.err
    assert not (path.parent / "runtime").exists()
    assert not (path.parent / ".pool.yaml.lock").exists()


def test_config_failure_skips_requested_gateway(pool_file, monkeypatch):
    path, _ = pool_file
    path.unlink()
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    report = doctor_report(str(path), gateway=True)
    assert check(report, "gateway")["status"] == "skip"
    assert "gateway" in report["not_checked"]


def test_disabled_account_key_is_not_read(pool_file, monkeypatch):
    path, data = pool_file
    settings = credentials(path)
    data["accounts"][1]["enabled"] = False
    save(path, data)
    disabled = Path(settings.accounts[1].credential_file)
    disabled.unlink()
    disabled.symlink_to(path.parent / "must-not-follow")
    report = doctor_report(str(path))
    assert report["ok"] is True
    assert check(report, "account_credential", "go-b")["code"] == "account_disabled"
    assert check(report, "enabled_accounts")["count"] == 1


def test_no_enabled_accounts_is_not_success(pool_file):
    path, data = pool_file
    credentials(path)
    for account in data["accounts"]:
        account["enabled"] = False
    save(path, data)
    report = doctor_report(str(path))
    assert report["ok"] is False
    assert check(report, "enabled_accounts")["code"] == "no_enabled_accounts"


@pytest.mark.parametrize("same_group", [False, True])
def test_duplicate_keys_follow_gateway_quota_group_rules(pool_file, same_group):
    path, data = pool_file
    settings = credentials(path)
    write_secret(Path(settings.accounts[1].credential_file), KEYS[0], replace=True)
    if same_group:
        data["accounts"][1]["quota_group"] = data["accounts"][0]["quota_group"]
        save(path, data)
    report = doctor_report(str(path))
    assert report["ok"] is same_group
    assert check(report, "credential_groups")["status"] == ("pass" if same_group else "fail")
    from pbatch.llm_pool_gateway import _load_keys
    if same_group:
        assert len(_load_keys(load_config(str(path)))) == 2
    else:
        with pytest.raises(ValueError):
            _load_keys(load_config(str(path)))


def test_reused_provider_key_never_sent_as_local_probe_token(pool_file, monkeypatch):
    path, _ = pool_file
    settings = credentials(path)
    write_secret(settings.token_file, KEYS[0], replace=True)
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    report = doctor_report(str(path), gateway=True)
    assert report["ok"] is False
    assert check(report, "account_credential", "go-a")["code"] == "gateway_token_reused"
    assert check(report, "gateway")["status"] == "skip"


@pytest.mark.parametrize("kind", ["public-file", "public-directory", "symlink", "hardlink",
                                 "fifo", "too-long", "invalid-ascii", "header-injection"])
def test_unsafe_credentials_are_aggregated_and_never_repaired(pool_file, kind):
    path, _ = pool_file
    settings = credentials(path)
    key = Path(settings.accounts[0].credential_file)
    if kind == "public-file":
        key.chmod(0o644)
    elif kind == "public-directory":
        key.parent.chmod(0o755)
    elif kind == "symlink":
        key.unlink()
        key.symlink_to(path.parent / "outside")
    elif kind == "hardlink":
        (key.parent / "another-link").hardlink_to(key)
    elif kind == "fifo":
        key.unlink()
        os.mkfifo(key, mode=0o600)
    else:
        key.write_bytes({"too-long": b"x" * 4098, "invalid-ascii": b"\xff" * 20,
                         "header-injection": b"x" * 20 + b"\nInjected: value"}[kind])
    report = doctor_report(str(path))
    assert report["ok"] is False
    assert check(report, "account_credential", "go-a")["code"] == "credential_unavailable"
    if kind == "public-file":
        assert key.stat().st_mode & 0o777 == 0o644
    if kind == "public-directory":
        assert key.parent.stat().st_mode & 0o777 == 0o755
    assert not settings.state_file.exists()


def test_exception_contents_are_never_printed(pool_file, monkeypatch, capsys):
    path, _ = pool_file
    secret = "raw-exception-credential-123456789"
    def failing_read(_):
        raise ValueError(secret)
    monkeypatch.setattr("pbatch.llm_pool_doctor.read_secret", failing_read)
    assert pool_main(["--config", str(path), "doctor", "--json"]) == 1
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert output.out.count("credential_unavailable") == 3


def test_real_cli_help_and_missing_config_exit_without_user_state(tmp_path):
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "pi-batch.py"), "pool"]
    help_result = subprocess.run([*command, "doctor", "--help"], capture_output=True,
                                 text=True, timeout=10, check=False, cwd=tmp_path)
    assert help_result.returncode == 0 and "--gateway" in help_result.stdout
    missing = tmp_path / "missing" / "pool.yaml"
    result = subprocess.run([*command, "--config", str(missing), "doctor", "--json"],
                            capture_output=True, text=True, timeout=10, check=False, cwd=tmp_path)
    assert result.returncode == 1 and json.loads(result.stdout)["ok"] is False
    assert not missing.parent.exists() and not result.stderr
