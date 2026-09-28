"""Ensure packaged YAML guard checks run real commands and reject false success."""

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "smoke-binary.py"
SPEC = importlib.util.spec_from_file_location("yaml_binary_smoke", SCRIPT)
SMOKE = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPT.parent))
try:
    SPEC.loader.exec_module(SMOKE)
finally:
    sys.path.remove(str(SCRIPT.parent))


def test_yaml_guard_smoke_uses_real_source_cli(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = SMOKE.isolated_environment(tmp_path)
    report = {"checks": []}
    token = SMOKE._REPORT.set(report)
    try:
        SMOKE.check_yaml_inputs([sys.executable, str(ROOT / "pi-batch.py")], workspace,
                                environment, 10, execute=SMOKE.run_command)
    finally:
        SMOKE._REPORT.reset(token)
    assert len(report["checks"]) == 4
    assert all(check["status"] == "passed" for check in report["checks"])


@pytest.mark.parametrize("failure", ["missing-budget", "leaked-input", "provider-started"])
def test_false_rejection_cannot_pass_yaml_smoke(tmp_path, failure):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = SMOKE.isolated_environment(tmp_path)

    def false_cli(_command, _cwd, _env, _timeout, label, **kwargs):
        output = "Tasks: 2" if "merged tasks" in label else "expanded node count"
        if "task alias budget" in label:
            if failure == "missing-budget":
                output = "some other error"
            elif failure == "leaked-input":
                output += " binary-yaml-private"
            else:
                (workspace.parent / "yaml-checks" / "agent-started").touch()
        kwargs["validate"](output)

    with pytest.raises(RuntimeError):
        SMOKE.check_yaml_inputs(["unused"], workspace, environment, 10, execute=false_cli)
