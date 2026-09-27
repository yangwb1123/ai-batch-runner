"""Regression tests for configuration provenance and version governance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pbatch import config
from pbatch.config_cli import config_main
from pbatch.config_inspect import config_check, explain


ROOT = Path(__file__).resolve().parents[1]


def test_explain_reports_sources_and_resolved_runtime():
    report = explain()
    assert report["status"] == "ok"
    assert report["schema_version"] == 1
    assert report["precedence"][0].startswith("CLI")
    assert report["resolved_runtime"]["agent.bin"]
    assert report["sources"][0]["name"] == "project"
    assert report["provenance"]["agent"]["children"]["bin"]["source"] == "project"


def test_first_run_missing_user_settings_is_optional_and_read_only(tmp_path, monkeypatch):
    from pbatch import user_dirs
    missing = tmp_path / "not-created" / "settings.yaml"
    monkeypatch.setattr(user_dirs, "user_settings_path", lambda: missing)
    report = explain()
    assert report["status"] == "ok"
    user = next(source for source in report["sources"] if source["name"] == "user")
    assert user["path"] == str(missing)
    assert not user["present"] and not user["loaded"] and not user["errors"]
    assert not missing.parent.exists()


def test_explicit_missing_config_still_fails_closed(tmp_path):
    assert config_check(str(tmp_path / "missing.yaml"))["valid"] is False


def test_existing_invalid_user_settings_still_fails_closed(tmp_path, monkeypatch):
    from pbatch import user_dirs
    settings = tmp_path / "settings.yaml"
    settings.write_text("invalid: [\n", encoding="utf-8")
    monkeypatch.setattr(user_dirs, "user_settings_path", lambda: settings)
    assert explain()["status"] == "invalid"


def test_explain_redacts_secret_looking_values(tmp_path):
    path = tmp_path / "pi-batch.yaml"
    path.write_text(
        "config_version: 1\nagent:\n  bin: fake\n  api_token: do-not-print\n",
        encoding="utf-8",
    )
    report = explain(str(path))
    assert report["status"] == "ok"
    assert report["effective_file_config"]["agent"]["api_token"] == "<redacted>"
    assert "do-not-print" not in json.dumps(report)


def test_config_check_rejects_newer_schema(tmp_path):
    path = tmp_path / "future.yaml"
    path.write_text("config_version: 99\nagent: {}\n", encoding="utf-8")
    report = config_check(str(path))
    assert report["valid"] is False
    assert any("newer than supported" in item
               for item in report["validation"]["errors"])


def test_runtime_loader_rejects_newer_schema(tmp_path, monkeypatch):
    path = tmp_path / "pi-batch.yaml"
    path.write_text("config_version: 99\n", encoding="utf-8")
    monkeypatch.setenv("PBATCH_SCRIPT_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="newer than supported"):
        config._find_batch_config()


def test_validator_registry_uses_merged_project_user_config(monkeypatch):
    merged = config._merge_config(
        {"validators": {"shared": "project-command", "project_only": "p"}},
        {"validators": {"shared": "user-command", "user_only": "u"}},
    )
    monkeypatch.setattr(config, "_BATCH_CFG", merged)
    registry = config._load_validators()
    assert registry == {
        "shared": "project-command",
        "project_only": "p",
        "user_only": "u",
    }


def test_config_check_rejects_malformed_mapping(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("config_version: 1\nagent: []\n", encoding="utf-8")
    report = config_check(str(path))
    assert report["valid"] is False
    assert any("agent" in item for item in report["validation"]["errors"])


def test_config_cli_json_exit_code_for_invalid_file(tmp_path, capsys):
    path = tmp_path / "future.yaml"
    path.write_text("config_version: 2\n", encoding="utf-8")
    assert config_main(["check", "--file", str(path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False


def test_config_cli_json_rejects_unknown_top_level_key(tmp_path, capsys):
    path = tmp_path / "typo.yaml"
    path.write_text(
        "config_version: 1\nagnt:\n  bin: silently-ignored\n",
        encoding="utf-8",
    )
    assert config_main(["check", "--file", str(path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert any("unknown top-level key" in item and "agnt" in item
               for item in payload["validation"]["errors"])


def test_config_cli_json_accepts_declared_top_level_keys(tmp_path, capsys):
    path = tmp_path / "valid.yaml"
    path.write_text(
        "config_version: 1\nagent:\n  bin: pi\nui_scan_dir: lib\n",
        encoding="utf-8",
    )
    assert config_main(["check", "--file", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["validation"]["errors"] == []


def test_config_subcommand_dispatches_from_entrypoint():
    result = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "config", "check", "--json"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"] is True
