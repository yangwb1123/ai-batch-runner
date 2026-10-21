"""Check declared validator startup references before spending model calls."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

import pytest

from pbatch import config
from pbatch.campaign import CampaignContext
from pbatch.campaign_models import CampaignSettings
from pbatch.campaign_preflight import _preflight_validators
from pbatch.campaign_state import StateStore
from pbatch.config import ValidatorSpec


def _context(root: Path, stages: list[dict]):
    # Task fixtures must use from_outputs: from_prompt ignores task templates.
    prepared = []
    for stage in stages:
        stage = dict(stage)
        if stage.get("tasks") and stage.get("from_prompt"):
            source = stage["name"] + "-source"
            prepared.append({"name": source, "from_prompt": "source", "output": "source.md"})
            stage.pop("from_prompt")
            stage["from_outputs"] = source
        prepared.append(stage)
    template = root / "pipeline.yaml"
    template.write_text(config.yaml.safe_dump({"stages": prepared}), encoding="utf-8")
    return CampaignContext(
        root, CampaignSettings(pipeline_template="pipeline.yaml"),
        argparse.Namespace(preflight_strict=False), StateStore(root / "state.jsonl"), {})


def _stage(validator: str) -> dict:
    return {"name": "implement", "from_prompt": "implement", "output": "out.md",
            "validate": validator}


def _python(script: str) -> str:
    return f"{shlex.quote(sys.executable)} {script} {{output}}"


@pytest.mark.parametrize("scope", ["file", "repo"])
def test_missing_registered_python_script_is_config_error_not_baseline_red(
        monkeypatch, tmp_path, caplog, scope):
    monkeypatch.setitem(config.VALIDATOR_SPECS, "missing_checker", ValidatorSpec(
        cmd=_python("{cwd}/missing-checker.py"), scope=scope))
    context = _context(tmp_path, [_stage("missing_checker")])

    with pytest.raises(SystemExit) as caught:
        _preflight_validators(context)

    assert caught.value.code == 4
    assert "missing-checker.py" in caplog.text
    assert "implement" in caplog.text


def test_unknown_bare_validator_is_rejected_before_analysis(tmp_path, caplog):
    name = "pbatch-deliberately-missing-validator-901b5b"
    context = _context(tmp_path, [_stage(name)])

    with pytest.raises(SystemExit) as caught:
        _preflight_validators(context)

    assert caught.value.code == 4
    assert name in caplog.text


@pytest.mark.parametrize("field", ["task", "gate_fix"])
def test_task_and_gate_fix_validators_are_also_preflighted(monkeypatch, tmp_path, field):
    monkeypatch.setitem(config.VALIDATOR_SPECS, "missing_nested", ValidatorSpec(
        cmd=_python("{cwd}/missing-nested.py")))
    stage = _stage("")
    if field == "task":
        stage["tasks"] = [{"prompt": "p", "output": "o", "validate": "missing_nested"}]
    else:
        stage["gate"] = True
        stage["gate_fix_rounds"] = 1
        stage["gate_fix_validate"] = "missing_nested"

    with pytest.raises(SystemExit) as caught:
        _preflight_validators(_context(tmp_path, [stage]))

    assert caught.value.code == 4


def test_readiness_does_not_execute_file_validator_or_need_future_output(monkeypatch, tmp_path):
    marker = tmp_path / "checker-ran"
    checker = tmp_path / "checker with spaces.py"
    checker.write_text(f"open({str(marker)!r}, 'w').write('unexpected')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "exists_only", ValidatorSpec(
        cmd=_python("{cwd}/'checker with spaces.py'")))

    assert _preflight_validators(_context(tmp_path, [_stage("exists_only")])) == 0
    assert not marker.exists()
    assert not (tmp_path / "out.md").exists()


def test_task_relative_cwd_is_used_for_script_readiness(monkeypatch, tmp_path):
    task_root = tmp_path / "task-root"
    task_root.mkdir()
    (task_root / "check.py").write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "relative_checker", ValidatorSpec(
        cmd=_python("check.py")))
    stage = _stage("")
    stage["tasks"] = [{"prompt": "p", "output": "o", "cwd": "task-root",
                       "validate": "relative_checker"}]
    assert _preflight_validators(_context(tmp_path, [stage])) == 0


def test_non_applicable_validator_does_not_require_its_script(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "optional_checker", ValidatorSpec(
        cmd=_python("missing.py"), requires_any=("Cargo.toml",)))
    assert _preflight_validators(_context(tmp_path, [_stage("optional_checker")])) == 0
    assert "not applicable" in caplog.text


def test_compound_shell_is_reported_as_partial_not_executed(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    marker = tmp_path / "must-not-run"
    command = f"touch {shlex.quote(str(marker))} && true"
    monkeypatch.setitem(config.VALIDATOR_SPECS, "compound_checker", ValidatorSpec(cmd=command))
    assert _preflight_validators(_context(tmp_path, [_stage("compound_checker")])) == 0
    assert "not statically checked" in caplog.text
    assert not marker.exists()


def test_python_module_mode_is_not_misread_as_script_path(monkeypatch, tmp_path):
    monkeypatch.setitem(config.VALIDATOR_SPECS, "module_checker", ValidatorSpec(
        cmd=f"{shlex.quote(sys.executable)} -m pytest {{output}}"))
    assert _preflight_validators(_context(tmp_path, [_stage("module_checker")])) == 0


def test_valid_raw_shell_builtin_remains_supported(tmp_path):
    assert _preflight_validators(_context(tmp_path, [_stage("true")])) == 0


def test_malformed_shell_quote_is_rejected_before_analysis(tmp_path):
    context = _context(tmp_path, [_stage("python 'unterminated")])
    with pytest.raises(SystemExit) as caught:
        _preflight_validators(context)
    assert caught.value.code == 4


@pytest.mark.parametrize("primary", ["", "true"])
def test_task_public_validate_alias_wins_over_legacy(tmp_path, primary):
    stage = _stage("")
    stage["tasks"] = [{"prompt": "p", "output": "o", "validate": primary,
                       "validate_cmd": "pbatch-deliberately-missing-legacy-validator"}]
    assert _preflight_validators(_context(tmp_path, [stage])) == 0


def test_missing_validator_blocks_campaign_before_analysis(monkeypatch, tmp_path):
    from pbatch import campaign_exec

    monkeypatch.setitem(config.VALIDATOR_SPECS, "missing_checker", ValidatorSpec(
        cmd=_python("{cwd}/missing-checker.py")))
    context = _context(tmp_path, [_stage("missing_checker")])
    context.args.modules = ""
    context.args.dry_run = False
    monkeypatch.setattr(campaign_exec, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(campaign_exec, "discover_modules", lambda *a: ["src"])
    monkeypatch.setattr(campaign_exec, "_campaign_plan", lambda *a: argparse.Namespace(
        selected=["src"], blocked=False))
    monkeypatch.setattr(campaign_exec, "_provider_breaker_admission", lambda *a: ("", []))
    calls = []
    monkeypatch.setattr(campaign_exec, "_analyze_modules", lambda *a: calls.append(a))

    with pytest.raises(SystemExit) as caught:
        campaign_exec.run_campaign(context)

    assert caught.value.code == 4
    assert calls == []


def test_task_declared_repo_validator_uses_stage_cwd(monkeypatch, tmp_path):
    (tmp_path / "subdir").mkdir()
    (tmp_path / "check.py").write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "root_checker", ValidatorSpec(
        cmd=_python("check.py"), scope="repo"))
    stage = _stage("")
    stage["tasks"] = [{"prompt": "p", "output": "o", "cwd": "subdir",
                       "validate": "root_checker"}]
    assert _preflight_validators(_context(tmp_path, [stage])) == 0


@pytest.mark.parametrize("manifest_in_root", [True, False])
def test_task_repo_applicability_uses_stage_root(monkeypatch, tmp_path, manifest_in_root):
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (tmp_path if manifest_in_root else subdir).joinpath("project.manifest").write_text(
        "project\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "root_optional", ValidatorSpec(
        cmd=_python("missing.py"), scope="repo", requires_any=("project.manifest",)))
    stage = _stage("")
    stage["tasks"] = [{"prompt": "p", "output": "o", "cwd": "subdir",
                       "validate": "root_optional"}]
    context = _context(tmp_path, [stage])
    if manifest_in_root:
        with pytest.raises(SystemExit) as caught:
            _preflight_validators(context)
        assert caught.value.code == 4
    else:
        assert _preflight_validators(context) == 0


@pytest.mark.parametrize("script", ["~/checks/gate.py", "checks/gate-*.py", "checks/gate?.py",
                                    "checks/gate[12].py"])
def test_shell_path_expansion_is_not_a_literal_missing_script(
        monkeypatch, tmp_path, caplog, script):
    monkeypatch.setitem(config.VALIDATOR_SPECS, "dynamic_path", ValidatorSpec(cmd=_python(script)))
    assert _preflight_validators(_context(tmp_path, [_stage("dynamic_path")])) == 0
    assert "not statically checked" in caplog.text


@pytest.mark.parametrize("checker_in_task", [True, False])
def test_inherited_file_validator_uses_task_cwd(monkeypatch, tmp_path, checker_in_task):
    task_root = tmp_path / "subdir"
    task_root.mkdir()
    (task_root if checker_in_task else tmp_path).joinpath("check.py").write_text(
        "raise RuntimeError('must not execute')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "inherited", ValidatorSpec(cmd=_python("check.py")))
    stage = _stage("inherited")
    stage["tasks"] = [{"prompt": "p", "output": "o", "cwd": "subdir", "validate": None}]
    context = _context(tmp_path, [stage])
    if checker_in_task:
        assert _preflight_validators(context) == 0
    else:
        with pytest.raises(SystemExit) as caught:
            _preflight_validators(context)
        assert caught.value.code == 4


@pytest.mark.parametrize("override", ["", "true"])
def test_explicit_task_override_does_not_preflight_unused_stage_file_validator(
        monkeypatch, tmp_path, override):
    monkeypatch.setitem(config.VALIDATOR_SPECS, "unused", ValidatorSpec(cmd=_python("absent.py")))
    stage = _stage("unused")
    stage["tasks"] = [{"prompt": "p", "output": "o", "validate": override}]
    assert _preflight_validators(_context(tmp_path, [stage])) == 0


@pytest.mark.parametrize("manifest_in_task", [True, False])
def test_inherited_file_applicability_uses_task_cwd(monkeypatch, tmp_path, manifest_in_task):
    task_root = tmp_path / "subdir"
    task_root.mkdir()
    (task_root if manifest_in_task else tmp_path).joinpath("project.manifest").write_text(
        "project\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "inherited_optional", ValidatorSpec(
        cmd=_python("absent.py"), requires_any=("project.manifest",)))
    stage = _stage("inherited_optional")
    stage["tasks"] = [{"prompt": "p", "output": "o", "cwd": "subdir"}]
    context = _context(tmp_path, [stage])
    if manifest_in_task:
        with pytest.raises(SystemExit) as caught:
            _preflight_validators(context)
        assert caught.value.code == 4
    else:
        assert _preflight_validators(context) == 0


@pytest.mark.parametrize("aggregate", [False, True])
@pytest.mark.parametrize("checker_in_input", [False, True])
def test_from_dir_file_validator_uses_input_cwd(
        monkeypatch, tmp_path, aggregate, checker_in_input):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs if checker_in_input else tmp_path).joinpath("check.py").write_text(
        "raise RuntimeError('must not execute')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "dir_checker", ValidatorSpec(cmd=_python("check.py")))
    context = _context(tmp_path, [{"name": "implement", "from_dir": "inputs",
                                   "aggregate": aggregate, "validate": "dir_checker"}])
    if checker_in_input:
        assert _preflight_validators(context) == 0
    else:
        with pytest.raises(SystemExit) as caught:
            _preflight_validators(context)
        assert caught.value.code == 4


def test_gate_fix_inherited_validator_still_uses_root(monkeypatch, tmp_path):
    task_root = tmp_path / "subdir"
    task_root.mkdir()
    (task_root / "check.py").write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "fix_inherit", ValidatorSpec(cmd=_python("check.py")))
    stage = _stage("fix_inherit")
    stage.update(gate=True, gate_fix_rounds=1)
    stage["tasks"] = [{"prompt": "p", "output": "o", "cwd": "subdir"}]
    with pytest.raises(SystemExit) as caught:
        _preflight_validators(_context(tmp_path, [stage]))
    assert caught.value.code == 4


def test_valid_shell_comment_with_apostrophe_is_not_bad_quoting(monkeypatch, tmp_path, caplog):
    monkeypatch.setitem(config.VALIDATOR_SPECS, "commented", ValidatorSpec(
        cmd=_python("check.py") + " # team's checker"))
    assert _preflight_validators(_context(tmp_path, [_stage("commented")])) == 0
    assert "not statically checked" in caplog.text


@pytest.mark.parametrize("source", ["task", "from_dir"])
def test_future_workdir_is_unverified_not_missing_executable(monkeypatch, tmp_path, caplog, source):
    checker = tmp_path / "check.py"
    checker.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "future_dir", ValidatorSpec(
        cmd=_python(shlex.quote(str(checker)))))
    if source == "task":
        stage = _stage("future_dir")
        stage["tasks"] = [{"prompt": "p", "output": "o", "cwd": "future-inputs"}]
    else:
        stage = {"name": "implement", "from_dir": "future-inputs", "validate": "future_dir"}
    assert _preflight_validators(_context(tmp_path, [stage])) == 0
    assert "not statically checked" in caplog.text
    assert not (tmp_path / "future-inputs").exists()


@pytest.mark.parametrize("nested_script", [True, False])
@pytest.mark.parametrize("declaration", ["stage", "task"])
def test_relative_cwd_placeholder_preserves_execution_spelling(
        monkeypatch, tmp_path, nested_script, declaration):
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    nested = subdir / "subdir"
    nested.mkdir()
    (nested if nested_script else subdir).joinpath("check.py").write_text(
        "raise RuntimeError('must not execute')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "relative_placeholder", ValidatorSpec(
        cmd=_python("{cwd}/check.py")))
    stage = _stage("relative_placeholder" if declaration == "stage" else "")
    stage["tasks"] = [{"prompt": "p", "output": "o", "cwd": "subdir"}]
    if declaration == "task":
        stage["tasks"][0]["validate"] = "relative_placeholder"
    context = _context(tmp_path, [stage])
    if nested_script:
        assert _preflight_validators(context) == 0
    else:
        with pytest.raises(SystemExit) as caught:
            _preflight_validators(context)
        assert caught.value.code == 4


def test_heredoc_quote_content_is_unverified_not_bad_direct_quoting(monkeypatch, tmp_path, caplog):
    monkeypatch.setitem(config.VALIDATOR_SPECS, "heredoc_checker", ValidatorSpec(
        cmd="grep -q foo <<EOF\nfoo'\nEOF"))
    assert _preflight_validators(_context(tmp_path, [_stage("heredoc_checker")])) == 0
    assert "not statically checked" in caplog.text


def test_python_directory_startup_is_unverified_not_missing_script(monkeypatch, tmp_path, caplog):
    package = tmp_path / "checks"
    package.mkdir()
    marker = tmp_path / "must-not-execute"
    (package / "__main__.py").write_text(
        f"open({str(marker)!r}, 'w').write('unexpected')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "python_directory", ValidatorSpec(
        cmd=_python("checks")))
    assert _preflight_validators(_context(tmp_path, [_stage("python_directory")])) == 0
    assert "not statically checked" in caplog.text
    assert not marker.exists()


@pytest.mark.parametrize("interpreter,argument,resolved", [
    ("node", "checks/gate", "checks/gate.js"), ("sh", "gate.sh", "bin/gate.sh")])
def test_interpreter_resolution_is_unverified_not_literal_missing_script(
        monkeypatch, tmp_path, caplog, interpreter, argument, resolved):
    marker = tmp_path / "interpreter-executed"
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    executable = binary_dir / interpreter
    executable.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n", encoding="utf-8")
    executable.chmod(0o755)
    target = tmp_path / resolved
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not executed\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setitem(config.VALIDATOR_SPECS, "interpreter_resolved", ValidatorSpec(
        cmd=f"{interpreter} {argument} {{output}}"))
    assert _preflight_validators(_context(tmp_path, [_stage("interpreter_resolved")])) == 0
    assert "not statically checked" in caplog.text
    assert not marker.exists()


def test_nested_from_dir_cwd_is_unverified_without_enumerating_inputs(monkeypatch, tmp_path, caplog):
    group = tmp_path / "inputs" / "group"
    group.mkdir(parents=True)
    (group / "doc.md").write_text("input\n", encoding="utf-8")
    (group / "check.py").write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "nested_input", ValidatorSpec(cmd=_python("check.py")))
    stage = {"name": "implement", "from_dir": "inputs", "suffix": "/*.md",
             "aggregate": False, "validate": "nested_input"}
    assert list((tmp_path / "inputs").glob("*" + stage["suffix"])) == [group / "doc.md"]
    assert _preflight_validators(_context(tmp_path, [stage])) == 0
    assert "not statically checked" in caplog.text


@pytest.mark.parametrize("checker_in_root", [False, True])
def test_nested_from_dir_aggregate_keeps_input_root_cwd(monkeypatch, tmp_path, checker_in_root):
    inputs = tmp_path / "inputs"
    group = inputs / "group"
    group.mkdir(parents=True)
    (group / "doc.md").write_text("input\n", encoding="utf-8")
    (inputs if checker_in_root else group).joinpath("check.py").write_text(
        "raise RuntimeError('must not execute')\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "aggregate_nested", ValidatorSpec(
        cmd=_python("check.py")))
    stage = {"name": "implement", "from_dir": "inputs", "suffix": "/*.md",
             "aggregate": True, "validate": "aggregate_nested"}
    context = _context(tmp_path, [stage])
    if checker_in_root:
        assert _preflight_validators(context) == 0
    else:
        with pytest.raises(SystemExit) as caught:
            _preflight_validators(context)
        assert caught.value.code == 4


def test_nested_input_deferral_does_not_skip_repo_references(monkeypatch, tmp_path):
    (tmp_path / "inputs").mkdir()
    monkeypatch.setitem(config.VALIDATOR_SPECS, "nested_repo", ValidatorSpec(
        cmd=_python("missing.py"), scope="repo"))
    stage = {"name": "implement", "from_dir": "inputs", "suffix": "/*.md",
             "validate": "nested_repo"}
    with pytest.raises(SystemExit) as caught:
        _preflight_validators(_context(tmp_path, [stage]))
    assert caught.value.code == 4


@pytest.mark.parametrize("scope", ["file", "repo"])
def test_output_derived_executable_is_unverified_not_invented_missing_path(
        monkeypatch, tmp_path, caplog, scope):
    from pbatch.campaign_pipeline import load_pipeline
    from pbatch.campaign_validator_preflight import validator_readiness_errors

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    marker = tmp_path / "checker-executed"
    checker = inputs / "gate.sh"
    checker.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n", encoding="utf-8")
    checker.chmod(0o755)
    (inputs / "doc.md").write_text("input\n", encoding="utf-8")
    monkeypatch.setitem(config.VALIDATOR_SPECS, "output_executable", ValidatorSpec(
        cmd="{output}/gate.sh", scope=scope))
    stage = {"name": "implement", "from_dir": "inputs", "validate": "output_executable"}
    context = _context(tmp_path, [stage])
    pipeline = load_pipeline(str(tmp_path / "pipeline.yaml"))
    # This unit check deliberately excludes pre-existing repo baseline execution,
    # whose output-directory expansion is not evidence about startup references.
    assert validator_readiness_errors(pipeline, tmp_path) == []
    if scope == "file":
        assert _preflight_validators(context) == 0
    assert "not statically checked" in caplog.text
    assert not marker.exists()
