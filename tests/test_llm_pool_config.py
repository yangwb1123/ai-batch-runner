"""Pool configuration, credentials and CLI must not expose or activate secrets."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from pbatch.llm_pool_config import load_config, parse_config
from pbatch.llm_pool_secrets import read_secret, write_secret
from pbatch.llm_pool_cli import pool_main


@pytest.fixture
def pool_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    data = {
        "schema_version": 1, "pool_id": "test-go",
        "models": {"glm-5.1": "chat/completions", "minimax-m2.7": "messages"},
        "gateway": {"token_file": str(tmp_path / "keys" / "local-token")},
        "accounts": [
            {"id": "go-a", "quota_group": "subscription-a", "credential_file": "keys/a"},
            {"id": "go-b", "quota_group": "subscription-b", "credential_file": "keys/b"},
        ],
    }
    path = tmp_path / "pool.yaml"
    path.write_text(yaml.safe_dump(data))
    return path, data


def test_read_only_check_and_client_config_do_not_create_state(pool_file, capsys):
    path, _ = pool_file
    assert pool_main(["--config", str(path), "check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["accounts"] == ["go-a", "go-b"]
    assert report["credentials_checked"] is False
    settings = load_config(str(path))
    assert settings.accounts[0].credential_file == str(path.parent / "keys" / "a")
    assert not settings.state_file.exists()
    assert not settings.token_file.exists()
    assert pool_main(["--config", str(path), "client-config", "--model", "glm-5.1"]) == 0
    client = json.loads(capsys.readouterr().out)
    options = client["provider"]["opencode-go"]["options"]
    assert options["baseURL"] == "http://127.0.0.1:8741/v1"
    assert options["apiKey"] == "{file:" + str(settings.token_file) + "}"
    assert not settings.state_file.exists()


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("pool_id", "../outside"),
    ("models", {"bad": []}), ("models", {"bad": "unknown"}), ("accounts", []),
    ("extra", "ignored"),
])
def test_invalid_configuration_fails_closed(pool_file, field, value):
    path, original = pool_file
    data = copy.deepcopy(original)
    data[field] = value
    with pytest.raises(ValueError):
        parse_config(data, path.parent)


@pytest.mark.parametrize("field,value", [
    ("request_timeout", float("nan")), ("request_timeout", float("inf")),
    ("request_timeout", 0), ("request_timeout", 10 ** 1000),
    ("port", True), ("port", 65536), ("max_connections", 0), ("probe_after", -1),
])
def test_gateway_limits_are_finite_bounded_numbers(pool_file, field, value):
    path, data = pool_file
    data["gateway"][field] = value
    with pytest.raises(ValueError):
        parse_config(data, path.parent)


@pytest.mark.parametrize("change", ["id", "credential_file", "token", "unknown"])
def test_duplicate_identity_and_embedded_secrets_rejected(pool_file, change):
    path, data = pool_file
    if change in ("id", "credential_file"):
        data["accounts"][1][change] = data["accounts"][0][change]
    elif change == "token":
        data["gateway"]["token_file"] = data["accounts"][0]["credential_file"]
    else:
        data["accounts"][0]["api_key"] = "do-not-embed"
    with pytest.raises(ValueError):
        parse_config(data, path.parent)


@pytest.mark.parametrize("text", [
    "schema_version: 1\nschema_version: 1\n",
    "a: &a [*a]\n", "\xff", "x" * 65537,
])
def test_yaml_is_bounded_and_unambiguous(pool_file, text):
    path, _ = pool_file
    path.write_bytes(text.encode("latin1"))
    with pytest.raises(ValueError):
        load_config(str(path))


def test_private_secret_roundtrip_and_explicit_replacement(tmp_path):
    path = tmp_path / "keys" / "key"
    first, second = "first-secret-123456789", "replacement-secret-123456789"
    write_secret(path, first)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert read_secret(path) == first
    with pytest.raises(FileExistsError):
        write_secret(path, second)
    assert read_secret(path) == first
    write_secret(path, second, replace=True)
    assert read_secret(path) == second
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("value", ["short", "a" * 4097, "bad\n" + "x" * 20,
                                 "x" * 20 + "\rheader", "x" * 20 + " ", "密钥" * 20])
def test_secret_rejects_header_injection_and_unbounded_values(tmp_path, value):
    with pytest.raises(ValueError):
        write_secret(tmp_path / "keys" / "key", value)


def test_secret_rejects_symlinks_hardlinks_and_public_permissions(tmp_path):
    path = tmp_path / "keys" / "key"
    write_secret(path, "original-secret-123456789")
    link = path.parent / "symlink"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        read_secret(link)
    with pytest.raises(OSError):
        write_secret(link, "replacement-secret-123456789", replace=True)
    hard = path.parent / "hardlink"
    hard.hardlink_to(path)
    with pytest.raises(ValueError):
        read_secret(hard)
    hard.unlink()
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_secret(path)


def test_cli_writes_credentials_without_printing_them(pool_file, monkeypatch, capsys):
    import io
    path, _ = pool_file
    args = ["--config", str(path)]
    assert pool_main(args + ["token-init"]) == 0
    settings = load_config(str(path))
    token = read_secret(settings.token_file)
    assert token not in capsys.readouterr().out
    key = "go-secret-123456789012345"
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    assert pool_main(args + ["key-set", "go-a", "--stdin"]) == 0
    assert key not in capsys.readouterr().out
    assert read_secret(Path(settings.accounts[0].credential_file)) == key


def test_cli_errors_do_not_echo_bad_yaml_secret(pool_file, capsys):
    path, _ = pool_file
    secret = "never-print-this-credential"
    path.write_text("accounts: [" + secret)
    assert pool_main(["--config", str(path), "check"]) == 2
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert "Traceback" not in output.err


def test_registered_cli_help_does_not_require_a_pool_config():
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([sys.executable, "pi-batch.py", "pool", "--help"],
                               cwd=root, text=True, capture_output=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert "client-config" in completed.stdout
    assert "mcp" in completed.stdout


def test_default_config_reference_and_unknown_model_are_fail_closed(pool_file, monkeypatch):
    from pbatch import config
    path, _ = pool_file
    monkeypatch.setattr(config, "_section", lambda _: {"config_file": str(path)})
    assert load_config().pool_id == "test-go"
    monkeypatch.setattr(config, "_section", lambda _: {"config_file": []})
    with pytest.raises(ValueError):
        load_config()
    assert pool_main(["--config", str(path), "status", "--model", "unknown"]) == 2
    assert not load_config(str(path)).state_file.exists()


def test_project_config_inspection_validates_pool_reference():
    from pbatch.config_inspect import validate_document
    assert validate_document({"config_version": 1, "llm_pool": {
        "config_file": "/private/pool.yaml"}})["valid"]
    for value in ({"config_file": []}, {"config_flie": "/typo"}, []):
        assert not validate_document({"config_version": 1, "llm_pool": value})["valid"]
