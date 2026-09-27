"""Frozen executables must launch their own CLI and bundled gate scripts."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import (advance, campaign_runtime, capsule, collab, config,
                    evolution, hub_compute_broker, isolate, memory, metering,
                    org, reflect, tools, ui_geometry)


@pytest.fixture
def frozen_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "unpacked"
    assets = bundle / "share" / "ai-batch-runner"
    scripts = assets / "scripts"
    scripts.mkdir(parents=True)
    for name in ("check-ui-spec.py", "check-baseline.py", "check-nongoal.py",
                 "check-changebudget.py", "check-ui-geometry.py"):
        (scripts / name).write_text("# bundled gate\n", encoding="utf-8")
    (assets / "quality.py").write_text("# bundled quality gate\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/pi batch/pi-batch")
    monkeypatch.setattr(config, "TOOL_ROOT", str(assets))
    return assets


def test_frozen_campaign_ignores_source_shim_and_preserves_run_id(
        frozen_bundle, tmp_path, monkeypatch):
    (tmp_path / "pi-batch.py").write_text("raise RuntimeError('old shim')\n")
    monkeypatch.setattr(metering, "RUN_ID", "parent-run")
    monkeypatch.setattr(metering, "EVENTS_FILE", "events.jsonl")
    command = campaign_runtime.isolated_command(
        tmp_path, tmp_path / "pipeline.yaml", tmp_path / "run.log",
        ["--memory-mode", "off"])
    assert command[:2] == [sys.executable, str(tmp_path / "pipeline.yaml")]
    assert command[command.index("--run-id") + 1] == "parent-run"
    assert command[command.index("--events-file") + 1] == "events.jsonl"
    assert command[-2:] == ["--memory-mode", "off"]
    assert str(tmp_path / "pi-batch.py") not in command


def test_frozen_campaign_child_uses_binary_with_worktree_cwd(
        frozen_bundle, tmp_path, monkeypatch):
    calls = []

    class Child:
        def wait(self, timeout):
            return 0

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Child()

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = campaign_runtime.run_isolated_child(
        tmp_path, tmp_path / "pipeline.yaml", tmp_path / "run.log", [], 10)
    assert result == (0, False)
    command, kwargs = calls[0]
    assert command[:2] == [sys.executable, str(tmp_path / "pipeline.yaml")]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["start_new_session"] is True


def test_frozen_collaboration_launches_isolate_without_python_flags(
        frozen_bundle, tmp_path):
    plan = collab.AgentPlan("worker", "repair", ("src",), ("src",), 20)
    args = argparse.Namespace(agent_bin="fake-agent", model="", provider="", retries=0)
    command = collab._child_command(
        plan, tmp_path, tmp_path / "worker.patch", args, tmp_path / "run.log", "worker")
    assert command[:6] == [sys.executable, "isolate", "-b", "worker", "-d", str(tmp_path)]
    assert "--no-git-commit" in command
    assert command[-2:] == ["--agent-bin", "fake-agent"]


def test_frozen_isolate_child_keeps_protected_mode(frozen_bundle, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs:
                        calls.append((argv, kwargs)) or SimpleNamespace(returncode=7))
    assert isolate._run_child(tmp_path, ["-p", "repair"], protected=True) == 7
    assert calls[0][0] == [sys.executable, "-p", "repair", "--no-git-commit"]
    assert calls[0][1]["cwd"] == str(tmp_path)


@pytest.mark.parametrize("consumer,expected", [
    ("capsule", ["eval"]), ("tools", ["health", "--help"]),
])
def test_frozen_cli_selfchecks_use_binary(frozen_bundle, monkeypatch, consumer, expected):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs:
                        calls.append(argv) or SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    if consumer == "capsule":
        assert capsule._run_eval() == (True, "ok")
    else:
        assert tools._check_dispatch({"health": {"target": "health:health_main"}}) == []
    assert calls == [[sys.executable, *expected]]


def test_frozen_prompts_quote_binary_and_skip_source_shims(frozen_bundle, tmp_path, monkeypatch):
    (tmp_path / "pi-batch.py").write_text("# source shim\n", encoding="utf-8")
    monkeypatch.setenv("PBATCH_SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MEMORY_MODE", "on")
    monkeypatch.setattr(memory, "_manifest_history", lambda *_args: [])
    manifest = memory.memory_manifest("continue", str(tmp_path))
    assert f"{shlex.quote(sys.executable)} memory recent" in manifest
    assert "pi-batch.py" not in manifest
    command = hub_compute_broker._tool_prompt("inspect", tmp_path / "capability.json")
    assert f"{shlex.quote(sys.executable)} fleet compute --via" in command
    assert " -m pbatch" not in command


def test_frozen_advance_uses_bundle_relative_checker(frozen_bundle):
    command = advance._checker_args(str(frozen_bundle / "scripts/check-ui-spec.py"), ".")
    assert command == [sys.executable, "--_bundled-script", "scripts/check-ui-spec.py",
                       "--dir", ".", "--all", "--json"]


def test_frozen_advance_rejects_external_script(frozen_bundle, tmp_path):
    checker = tmp_path / "check-ui-spec.py"
    checker.write_text("raise RuntimeError('external script')\n", encoding="utf-8")
    with pytest.raises(advance.CheckerError, match="execution failed"):
        advance._run_checker(str(checker), ".")


def test_frozen_advance_does_not_replace_project_checker(frozen_bundle, tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    checker = scripts / "check-ui-spec.py"
    checker.write_text("# project checker\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    selected = advance._checker_script(checker.name)
    assert selected == str(checker)
    with pytest.raises(advance.CheckerError, match="execution failed"):
        advance._run_checker(selected, ".")


def test_frozen_evolution_runs_bundled_checks_and_cleans_artifact(
        frozen_bundle, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs:
                        calls.append(argv) or SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setattr(evolution, "_append_state", lambda *_args: None)
    assert evolution._cmd_check(argparse.Namespace(dir=str(tmp_path), task_text="repair")) == 0
    assert [argv[:3] for argv in calls] == [
        [sys.executable, "--_bundled-script", f"scripts/check-{name}.py"]
        for name in ("baseline", "nongoal", "changebudget")]
    assert not Path(calls[1][3]).exists()


def test_frozen_org_normalizes_only_owned_commands(frozen_bundle):
    assert org._normalize_check_argv(["pi-batch", "org", "check"]) == [
        sys.executable, "org", "check"]
    assert org._normalize_check_argv(["scripts/check-baseline.py", "."]) == [
        sys.executable, "--_bundled-script", "scripts/check-baseline.py", "."]
    assert org._normalize_check_argv(["python", "custom.py"]) == ["python", "custom.py"]


def test_frozen_org_reports_external_script_as_failed(frozen_bundle, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/check-baseline.py").write_text("# project override\n")
    result = org._execute_check("scripts/check-baseline.py .", {})
    assert result["status"] == "failed"
    assert result["reason"]


def test_frozen_geometry_preserves_gate_output_and_exit(frozen_bundle, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs:
                        calls.append(argv) or SimpleNamespace(returncode=1, stdout="finding\n", stderr=""))
    with pytest.raises(SystemExit) as exc:
        ui_geometry.ui_geometry_main(["--dir", "src", "--all", "--json"])
    assert exc.value.code == 1
    assert calls == [[sys.executable, "--_bundled-script", "scripts/check-ui-geometry.py",
                      "--dir", "src", "--all", "--json"]]
    assert capsys.readouterr().out == "finding\n"


def test_frozen_reflect_uses_bundled_quality_script(frozen_bundle, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs:
                        calls.append(argv) or SimpleNamespace(
                            returncode=1, stdout="large.py:1 func 'run' 60 lines > 50", stderr=""))
    findings = reflect._complexity_check("src")
    assert calls == [[sys.executable, "--_bundled-script", "quality.py", "--strict", "src"]]
    assert findings[0]["type"] == "over_design"
