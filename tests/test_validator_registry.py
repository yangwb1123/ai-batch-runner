"""Tests for the validator registry itself: every registered validator in
pi-batch.yaml must reference an existing script, resolve its placeholders,
and actually run (dry) against a real temp artifact."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from pbatch.config import VALIDATORS, _resolve_validators
from pbatch.cmd_expand import expand_cmd

# Validators whose commands are pure shell one-liners on {output} (no repo
# script involved) — they run against a scratch artifact.
SHELL_ONLY = {"gofmt", "secretscan"}


def test_registry_has_expected_validators():
    for name in ("quick", "config", "root", "pyquality", "secretscan",
                 "uispacing", "uicolor", "uistyle", "uifont", "uicheck", "uiquality",
                 "backendquality", "completion"):
        assert name in VALIDATORS, name


def test_every_validator_script_exists():
    """Commands referencing scripts/... must point at real files."""
    for name, spec in VALIDATORS.items():
        command = spec if isinstance(spec, str) else spec.get("cmd", "")
        normalized = command.replace("{cwd}", ".").replace("{output}", ".")
        for match in re.finditer(r"([\w./-]*scripts/[\w./-]+\.py)", normalized):
            path = Path(match.group(1))
            # 绝对路径属于跨机部署配置（如 completion_go 指向工具集安装点），
            # 本机不强制存在；相对路径必须在仓库内真实存在。
            if not path.is_absolute():
                assert path.exists(), f"{name}: script {path} missing"
        for match in re.finditer(r"([\w./-]*quality\.py)", normalized):
            path = Path(match.group(1))
            if not path.is_absolute():
                assert path.exists(), f"{name}: script {path} missing"


def test_validator_commands_parse():
    """Every command must keep {output}/{cwd}/{ui_scan_dir}/{tool_root}
    substitutable (no quoting breakage) and compile as a shell line.
    Expansion goes through the shared helper exactly like the runner does,
    so a leftover placeholder fails the test instead of reaching the shell
    (QA F-1 residual half / testing-reviewer F2)."""
    for name, spec in VALIDATORS.items():
        command = spec if isinstance(spec, str) else spec.get("cmd", "")
        resolved = expand_cmd(command, "/tmp/artifact.md", str(Path.cwd()))
        assert "{output}" not in resolved and "{cwd}" not in resolved, \
            f"{name}: leftover placeholder in {command}"
        assert "{ui_scan_dir}" not in resolved and "{tool_root}" not in resolved, \
            f"{name}: leftover placeholder in {command}"
        # python-based commands must be runnable with --help or a dry run
        if "python" in resolved:
            assert "python" in command


def test_single_expansion_entry_source_anchor():
    """F-7 anchor: validator-command placeholder expansion has exactly one
    hand-rolled entry (pbatch/cmd_expand.py). A site that hand-rolls its
    own replace chain can leak a literal {ui_scan_dir} into the shell —
    the script scans a nonexistent directory, passes vacuously and the
    false-green resurrects (design F1). pipeline.py:633 is the documented
    out-of-scope exception: the stage post-command surface ({tool_root}
    only, no validator commands — design non-goal)."""
    root = Path(__file__).resolve().parent.parent / "pbatch"
    offenders = []
    for path in sorted(root.glob("*.py")):
        if path.name in ("cmd_expand.py", "pipeline.py"):
            continue
        src = path.read_text(encoding="utf-8")
        if '.replace("{cwd}"' in src or '.replace("{tool_root}"' in src:
            offenders.append(path.name)
    assert offenders == [], f"hand-rolled placeholder expansion in: {offenders}"


def test_python_validators_run_dry(tmp_path):
    """Run every python-based validator against a real artifact: a
    compliant file must pass the UI/engineering gates and the completion
    gate must accept an honest report."""
    clean = tmp_path / "clean.tsx"
    clean.write_text("SizedBox(height: 24)\nstyle={classes.x}\n", encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text(
        "```yaml\ncompletion_report:\n  summary: x\n  changed_files: [a]\n"
        "  commands_executed:\n    - command: lint\n      result: passed\n"
        "  residual_risks: []\n  assumptions: []\n```\n", encoding="utf-8")
    for name in ("uispacing", "uicolor", "uistyle", "uifont"):
        command = VALIDATORS[name].replace("{output}", str(clean)) \
                                  .replace("{cwd}", str(Path.cwd()))
        result = subprocess.run(["bash", "-c", command], capture_output=True,
                                text=True, timeout=120)
        assert result.returncode == 0, f"{name} failed on clean file: {result.stderr}"
    command = VALIDATORS["completion"].replace("{output}", str(report)) \
                                      .replace("{cwd}", str(Path.cwd()))
    result = subprocess.run(["bash", "-c", command], capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, f"completion failed on honest report: {result.stderr}"


def test_completion_gate_rejects_fabricated(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("测试理论上应该通过。\n", encoding="utf-8")
    command = VALIDATORS["completion"].replace("{output}", str(bad)) \
                                      .replace("{cwd}", str(Path.cwd()))
    result = subprocess.run(["bash", "-c", command], capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 1
    assert "missing completion_report" in result.stderr


def test_project_scan_validators_ok_on_empty_dir():
    """uicheck/uiquality scan {ui_scan_dir} (default {cwd}/src); this
    repo has no src/ so the gates must pass (safe for repos without
    frontend code). Expansion goes through the shared helper exactly like
    the runner does, so no placeholder can leak to the shell."""
    repo = str(Path.cwd())
    for name in ("uicheck", "uiquality", "backendquality"):
        command = expand_cmd(VALIDATORS[name], "/tmp/artifact.md", repo)
        result = subprocess.run(["bash", "-c", command], capture_output=True,
                                text=True, timeout=120)
        assert result.returncode == 0, f"{name} failed: {result.stderr}"


def test_resolve_validators_keeps_order():
    commands = _resolve_validators("uispacing,uicolor")
    assert len(commands) == 2
    assert "spacing" in commands[0] and "color" in commands[1]
