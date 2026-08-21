"""Regression tests for explicit validator applicability conditions."""

from __future__ import annotations

import logging
from pathlib import Path

from pbatch import config
from pbatch.config import ValidatorSpec
from pbatch.validation_batch import _file_commands


def test_requires_any_matches_a_file_under_the_target_root(tmp_path: Path):
    spec = ValidatorSpec(
        cmd="cargo check --quiet", scope="repo", requires_any=("Cargo.toml",)
    )
    assert not config.validator_applies(spec, str(tmp_path))
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
    assert config.validator_applies(spec, str(tmp_path))


def test_validator_mapping_parses_requires_any():
    spec = config._validator_spec({
        "cmd": "cargo check --quiet",
        "scope": "repo",
        "requires_any": ["Cargo.toml", "**/Cargo.toml"],
    })
    assert spec is not None
    assert spec.requires_any == ("Cargo.toml", "**/Cargo.toml")


def test_file_validator_logs_and_skips_missing_prerequisite(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="pi-batch")
    spec = ValidatorSpec(
        cmd="false", requires_any=("package.json",), scope="file"
    )
    commands = _file_commands(
        [spec], str(tmp_path), str(tmp_path / "artifact.md"),
        lambda command, workdir, output: command,
        config.log,
        "VALIDATE",
    )
    assert commands == []
    assert "not applicable" in caplog.text


def test_explain_exposes_resolved_validator_applicability():
    from pbatch.config_inspect import explain

    spec = explain()["resolved_runtime"]["validators.specs"]["cargocheck"]
    assert spec["scope"] == "repo"
    assert spec["requires_any"] == ["Cargo.toml"]


def test_advance_reports_only_non_applicable_validator_as_skipped(tmp_path):
    from pbatch import advance

    result = advance._run_validators("cargocheck", tmp_path)
    assert result["success"] is True
    assert result["status"] == "SKIPPED"
    assert result["results"][0]["status"] == "SKIPPED"


def test_repo_validator_runs_when_prerequisite_exists(tmp_path, monkeypatch):
    from pbatch.repo_gates import run_stage_repo_validators
    from types import SimpleNamespace

    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
    monkeypatch.setitem(
        config.VALIDATOR_SPECS,
        "applicable_repo_gate",
        ValidatorSpec(cmd="false", scope="repo", requires_any=("Cargo.toml",)),
    )
    stage = SimpleNamespace(
        name="rust-stage", cwd=str(tmp_path), validate_cmd="applicable_repo_gate",
        tasks=[],
    )
    ok, detail = run_stage_repo_validators(stage, [], [])
    assert ok is False
    assert detail == "false"
