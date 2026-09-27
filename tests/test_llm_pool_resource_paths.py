"""Credentials cannot overwrite their configuration, SQLite state or edit locks."""

import io
from pathlib import Path
import sys

import pytest
import yaml

from pbatch.llm_pool_cli import pool_main
from pbatch.llm_pool_config import load_config, parse_config


@pytest.fixture
def config_data(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    return {"schema_version": 1, "pool_id": "test",
            "models": {"m": "chat/completions"},
            "gateway": {"token_file": "keys/local"},
            "accounts": [{"id": "a", "quota_group": "g", "credential_file": "keys/a"}]}


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal", "/nested-key"])
@pytest.mark.parametrize("resource", ["account", "token"])
def test_credential_and_token_cannot_overlap_sqlite(config_data, tmp_path, suffix, resource):
    settings = parse_config(config_data, tmp_path)
    value = str(settings.state_file) + suffix
    if resource == "account":
        config_data["accounts"][0]["credential_file"] = value
    else:
        config_data["gateway"]["token_file"] = value
    with pytest.raises(ValueError):
        parse_config(config_data, tmp_path)
    assert not settings.state_file.exists()


@pytest.mark.parametrize("reference", ["pool.yaml", "pool.yaml/key", ".pool.yaml.lock",
                                       ".pool.yaml.backup-123", ".pool.yaml.tmp-123"])
@pytest.mark.parametrize("resource", ["account", "token"])
def test_credential_replacement_cannot_overwrite_config_namespace(
        config_data, tmp_path, monkeypatch, capsys, reference, resource):
    path = tmp_path / "pool.yaml"
    if resource == "account":
        config_data["accounts"][0]["credential_file"] = reference
    else:
        config_data["gateway"]["token_file"] = reference
    path.write_text(yaml.safe_dump(config_data))
    path.chmod(0o600)
    before = path.read_bytes()
    monkeypatch.setattr(sys, "stdin", io.StringIO("fake-new-secret-never-saved\n"))
    action = ["key-set", "a", "--stdin", "--replace"] if resource == "account" else [
        "token-init", "--replace"]
    assert pool_main(["--config", str(path), *action]) == 2
    assert path.read_bytes() == before
    assert "fake-new-secret" not in capsys.readouterr().err
    with pytest.raises(ValueError):
        load_config(str(path))


def test_file_references_cannot_be_ancestors_of_other_files(config_data, tmp_path):
    config_data["accounts"][0]["credential_file"] = "keys"
    with pytest.raises(ValueError):
        parse_config(config_data, tmp_path)
    config_data["accounts"][0]["credential_file"] = "keys/a"
    config_data["accounts"].append({"id": "b", "quota_group": "h", "credential_file": "keys/a/key"})
    with pytest.raises(ValueError):
        parse_config(config_data, tmp_path)


def test_distinct_resources_under_same_directory_remain_supported(config_data, tmp_path):
    settings = parse_config(config_data, tmp_path)
    assert settings.token_file.parent == Path(settings.accounts[0].credential_file).parent
    assert not settings.token_file.exists()
