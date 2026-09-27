"""The release smoke must detect unavailable bundled gates and hung children."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "smoke-binary.py"
SPEC = importlib.util.spec_from_file_location("binary_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPT.parent))
try:
    SPEC.loader.exec_module(SMOKE)
finally:
    sys.path.remove(str(SCRIPT.parent))


def _health() -> dict:
    return {"assets": {"healthy": True, "missing": []},
            "checkers": {"designintelligence": 0, "knowledge": 0,
                         "backendquality": 0, "packaged_assets": 0},
            "quality_violations": 0, "tests": 0, "healthy": False,
            "score": {"score": 60, "grade": "C"}}


def test_health_accepts_runtime_probe_evidence_without_source_tests():
    SMOKE.check_health(json.dumps(_health()))


@pytest.mark.parametrize("probe", ["designintelligence", "knowledge", "backendquality"])
def test_health_rejects_unknown_bundled_checker(probe):
    report = _health()
    report["checkers"][probe] = -1
    with pytest.raises(SMOKE.SmokeFailure, match="unknown checker results"):
        SMOKE.check_health(json.dumps(report))


@pytest.mark.parametrize("change", ["missing_assets", "absent_checker", "unknown_quality", "boolean_quality"])
def test_health_rejects_missing_or_malformed_evidence(change):
    report = _health()
    if change == "missing_assets":
        report["assets"]["missing"] = ["quality.py"]
    elif change == "absent_checker":
        del report["checkers"]["knowledge"]
    else:
        report["quality_violations"] = False if change == "boolean_quality" else -1
    with pytest.raises(SMOKE.SmokeFailure):
        SMOKE.check_health(json.dumps(report))


def test_environment_removes_source_config_and_python_search_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", "/unrelated-user-state")
    monkeypatch.setenv("PBATCH_SCRIPT_DIR", str(ROOT))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("PYTHONHOME", "/unrelated-python")
    monkeypatch.setenv("GIT_DIR", "/unrelated-repository/.git")
    for key in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(key, "/unrelated-temporary-directory")
    env = SMOKE.isolated_environment(tmp_path)
    assert "PBATCH_DATA_DIR" not in env
    assert "PYTHONHOME" not in env
    assert "GIT_DIR" not in env
    assert env["PYTHONPATH"] == ""
    assert Path(env["PATH"]).is_dir() and not list(Path(env["PATH"]).iterdir())
    assert Path(env["PBATCH_RUNTIME_HOME"]).is_relative_to(tmp_path)
    assert env["PBATCH_SCRIPT_DIR"] == str(tmp_path / "workspace")
    for key in ("TMPDIR", "TEMP", "TMP"):
        assert env[key] == str(tmp_path / "extraction with spaces")
        assert Path(env[key]).is_dir()


def test_hung_command_is_killed_at_deadline(tmp_path):
    started = time.monotonic()
    with pytest.raises(SMOKE.SmokeFailure, match="deadline"):
        SMOKE.run_command([sys.executable, "-c", "import time; time.sleep(30)"],
                          tmp_path, dict(os.environ), 0.1, "hung checker")
    assert time.monotonic() - started < 5


def test_command_failure_exposes_label_and_diagnostic(tmp_path):
    with pytest.raises(SMOKE.SmokeFailure, match="broken checker: exit 7") as error:
        SMOKE.run_command([sys.executable, "-c", "import sys; print('probe failed'); sys.exit(7)"],
                          tmp_path, dict(os.environ), 10, "broken checker")
    assert "probe failed" in str(error.value)


def test_source_fake_provider_gates_and_pipeline_round_trip(tmp_path):
    tmp_path = tmp_path / "path with spaces"
    tmp_path.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = SMOKE.isolated_environment(tmp_path)
    # Source execution legitimately needs its external Python for checkers;
    # main() never restores PATH for standalone gate and pipeline checks.
    env["PATH"] = os.environ.get("PATH", "")
    command = [sys.executable, str(ROOT / "pi-batch.py")]
    config = SMOKE.run_command([*command, "config", "explain", "--json"],
                               workspace, env, 30, "config")
    SMOKE.prepare_fake_agent(workspace, config)
    SMOKE.check_tasks(command, workspace, env, 30)
    assert not (workspace / "rejected.json").exists()
    assert json.loads((workspace / "pipeline.json").read_text()) == {"ok": True}


def test_release_workflow_invokes_isolated_smoke():
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/binary-verify.yml" in release
    workflow = (ROOT / ".github/workflows/binary-verify.yml").read_text(encoding="utf-8")
    assert "python tools/smoke-binary.py" in workflow
    assert "dist/pi-batch-${{ matrix.asset }}${{ matrix.extension }}" in workflow


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_invalid_timeout_is_rejected_before_smoke(timeout):
    result = subprocess.run([sys.executable, str(SCRIPT), sys.executable, "--timeout", timeout],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "finite and positive" in result.stderr


@pytest.mark.parametrize("failure", [False, True])
def test_report_records_commands_skips_and_failure_without_claiming_live_model(tmp_path, monkeypatch, failure):
    executable, report_path = Path(sys.executable).resolve(), tmp_path / "evidence/smoke.json"

    def checks(_command, workspace, environment, timeout):
        SMOKE.record_step("tui PTY", "skipped", reason="platform unavailable")
        command = [sys.executable, "-c", "raise SystemExit(7)" if failure else "print('ok')"]
        SMOKE.run_command(command, workspace, environment, timeout, "fixture command")

    monkeypatch.setattr(SMOKE, "run_checks", checks)
    assert SMOKE.run_smoke(executable, 5, report_path) == int(failure)
    report = json.loads(report_path.read_text())
    assert report["status"] == ("failed" if failure else "passed")
    assert report["binary"] == SMOKE.binary_identity(executable)
    assert report["checks"][0]["status"] == "skipped"
    assert report["checks"][1]["status"] == report["status"]
    assert "no live model" in report["scope"]
    assert report["actual_seconds"] >= 0 and SMOKE._REPORT.get() is None
    if failure:
        assert "exit 7" in report["error"]


def test_smoke_rejects_changed_binary_and_report_cannot_overwrite_it(tmp_path, monkeypatch):
    executable = tmp_path / "binary"
    executable.write_bytes(b"original executable")
    monkeypatch.setattr(SMOKE, "run_checks", lambda *_args: executable.write_bytes(b"replaced"))
    report_path = tmp_path / "report.json"
    assert SMOKE.run_smoke(executable, 5, report_path) == 1
    assert "changed during verification" in json.loads(report_path.read_text())["error"]
    with pytest.raises(SystemExit) as error:
        SMOKE.main([str(executable), "--report", str(executable)])
    assert error.value.code == 2 and executable.read_bytes() == b"replaced"


def _assert_failed_check(report_path, label):
    report = json.loads(report_path.read_text())
    checks = [check for check in report["checks"] if check["name"] == label]
    assert report["status"] == "failed" and report["error"]
    assert len(checks) == 1 and checks[0]["status"] == "failed"
    assert checks[0]["reason"]


@pytest.mark.parametrize("failed_step, label", [
    ("health", "health probes"), ("config", "bundled validator configuration"),
])
def test_report_waits_for_health_and_config_semantics(tmp_path, monkeypatch, failed_step, label):
    def command_output(command, *_args):
        if command[1] == "health":
            return "{}" if failed_step == "health" else json.dumps(_health())
        if command[1] == "config":
            return "{}"
        return ""
    monkeypatch.setattr(SMOKE, "_command_output", command_output)
    report_path = tmp_path / "report.json"
    assert SMOKE.run_smoke(Path(sys.executable).resolve(), 5, report_path) == 1
    _assert_failed_check(report_path, label)


@pytest.mark.parametrize("failed_artifact, label", [
    ("result.json", "fake-provider and bundled validators"),
    ("rejected.json", "schema rejection"),
    ("pipeline.json", "pipeline and bundled validators"),
])
def test_report_waits_for_task_and_pipeline_artifact_semantics(
        tmp_path, monkeypatch, failed_artifact, label):
    def command_output(command, workspace, *_args):
        if "smoke-valid" in command and failed_artifact != "result.json":
            (workspace / "result.json").write_text('{"ok": true}')
        if "smoke-invalid" in command and failed_artifact == "rejected.json":
            (workspace / "rejected.json").write_text('{"ok": false}')
        return ""
    monkeypatch.setattr(SMOKE, "_command_output", command_output)
    monkeypatch.setattr(SMOKE, "run_checks", SMOKE.check_tasks)
    report_path = tmp_path / "report.json"
    assert SMOKE.run_smoke(Path(sys.executable).resolve(), 5, report_path) == 1
    _assert_failed_check(report_path, label)


def test_semantic_failure_after_successful_child_exit_records_failed_check(tmp_path, monkeypatch):
    def invalid_health(_command, workspace, env, timeout):
        SMOKE.run_command([sys.executable, "-c", "print('{}')"], workspace, env,
                          timeout, "health probes", validate=SMOKE.check_health)
    monkeypatch.setattr(SMOKE, "run_checks", invalid_health)
    report_path = tmp_path / "report.json"
    assert SMOKE.run_smoke(Path(sys.executable).resolve(), 5, report_path) == 1
    _assert_failed_check(report_path, "health probes")


@pytest.mark.skipif(os.name != "posix", reason="POSIX pseudo-terminal contract")
def test_pty_startup_failure_records_specific_failed_check(tmp_path, monkeypatch):
    def failed_tui(_command, workspace, env, timeout):
        SMOKE.check_tui([sys.executable, "-c", "raise SystemExit(9)"],
                        workspace, env, timeout)
    monkeypatch.setattr(SMOKE, "run_checks", failed_tui)
    report_path = tmp_path / "report.json"
    assert SMOKE.run_smoke(Path(sys.executable).resolve(), 5, report_path) == 1
    _assert_failed_check(report_path, "tui PTY render and Ctrl+Q exit")
