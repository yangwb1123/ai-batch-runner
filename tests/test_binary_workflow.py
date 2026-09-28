"""Binary CI must exercise each target and retain failures without publishing."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    return _workflow("binary-verify.yml")["jobs"]["verify"]["steps"]


def _plan(tmp_path: Path, platform: str = "all", source: str = "a" * 40):
    step = _workflow("binary-verify.yml")["jobs"]["plan"]["steps"][0]
    output = tmp_path / "outputs"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={**os.environ, "PLATFORM_SELECTION": platform, "SOURCE_SHA": source,
             "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True,
        timeout=10)
    values = (dict(line.split("=", 1) for line in output.read_text().splitlines())
              if output.exists() else {})
    return result, values


def test_binary_verification_can_run_without_a_release_or_write_token() -> None:
    workflow = _workflow("binary-verify.yml")
    triggers = workflow.get("on", workflow.get(True))  # PyYAML's YAML 1.1 boolean spelling.
    assert set(triggers) == {"workflow_call", "workflow_dispatch", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"plan", "verify"}
    assert "needs" not in workflow["jobs"]["plan"]
    assert workflow["jobs"]["verify"]["needs"] == "plan"
    for job in workflow["jobs"].values():
        assert "permissions" not in job
        assert "environment" not in job
        for step in job["steps"]:
            assert "gh release" not in step.get("run", "")
            assert "pypi-publish" not in step.get("uses", "")


def test_binary_verification_covers_runtime_and_packaged_asset_changes() -> None:
    workflow = _workflow("binary-verify.yml")
    triggers = workflow.get("on", workflow.get(True))
    paths = triggers["pull_request"]["paths"]
    for changed in ("pbatch/launcher.py", "pyproject.toml", "tools/build-binary.py",
                    "tools/smoke-binary.py", "tools/tui-soak.py", "tools/tui_soak_support.py", "quality.py",
                    "tools/binary_smoke_lock.py", "tools/binary_smoke_provider.py",
                    "tools/smoke-tui-signals.py", "tools/tui_signal_provider.py",
                    "tools/smoke-tui-recovery.py", "tools/tui_recovery_provider.py",
                    "tools/smoke-batch-signals.py", "tools/tui_soak_watch.py",
                    "tests/test_batch_signals.py", "tests/test_tui_unknown_recovery.py",
                    "tests/test_tui_soak_discovery.py",
                    "tests/test_file_lock.py",
                    "tests/test_process_identity.py", "tests/test_instance_lock_safety.py",
                    "tests/test_instance_lock_generation.py", "tests/test_lock_domain.py",
                    "ui-specs/rules.yaml", "backend-specs/rules.yaml",
                    "scripts/check-json-schema.py", ".github/workflows/release.yml",
                    ".github/workflows/release-binary.yml", "tools/binary_manifest.py",
                    "LICENSE", "tests/test_release_binary_workflow.py"):
        assert any(fnmatch.fnmatchcase(changed, pattern) for pattern in paths), changed


def test_binary_matrix_pins_native_build_platforms_and_python_architectures(tmp_path) -> None:
    job = _workflow("binary-verify.yml")["jobs"]["verify"]
    result, outputs = _plan(tmp_path)
    assert result.returncode == 0, result.stderr
    assert job["strategy"]["matrix"] == "${{ fromJSON(needs.plan.outputs.matrix) }}"
    targets = {row["asset"]: (row["os"], row["python-arch"], row["extension"])
               for row in json.loads(outputs["matrix"])["include"]}
    assert targets == {
        "linux-x86_64": ("ubuntu-22.04", "x64", ""),
        "macos-x86_64": ("macos-15-intel", "x64", ""),
        "macos-arm64": ("macos-15", "arm64", ""),
        "windows-x86_64": ("windows-2022", "x64", ".exe"),
    }
    assert job["strategy"]["fail-fast"] is False
    setup = next(step for step in _steps() if "setup-python@" in step.get("uses", ""))
    assert setup["with"]["architecture"] == "${{ matrix.python-arch }}"
    platform = next(step for step in _steps() if step.get("id") == "platform")
    assert 'if observed != report["expected"]:' in platform["run"]
    assert 'report["commit"] != os.environ["SOURCE_SHA"]' in platform["run"]
    checkout = next(step for step in _steps() if "checkout@" in step.get("uses", ""))
    assert checkout["with"]["ref"] == "${{ needs.plan.outputs.source_sha }}"


def test_reusable_verification_defaults_to_all_and_can_select_exact_linux_commit(tmp_path):
    workflow = _workflow("binary-verify.yml")
    triggers = workflow.get("on", workflow.get(True))
    for trigger in ("workflow_call", "workflow_dispatch"):
        assert triggers[trigger]["inputs"]["platform"]["default"] == "all"
        assert triggers[trigger]["inputs"]["source_sha"]["default"] == ""
    plan = workflow["jobs"]["plan"]["steps"][0]
    assert plan["env"]["SOURCE_SHA"] == "${{ inputs.source_sha || github.sha }}"
    result, outputs = _plan(tmp_path, "linux-x86_64", "b" * 40)
    assert result.returncode == 0, result.stderr
    assert outputs["source_sha"] == "b" * 40
    assert [row["asset"] for row in json.loads(outputs["matrix"])["include"]] == [
        "linux-x86_64"]


@pytest.mark.parametrize("platform,source", [
    ("linux", "a" * 40), ("all", "main"), ("all", "a" * 39),
    ("linux-x86_64", "a" * 40 + "\nsource_sha=other"),
])
def test_verification_rejects_unknown_platform_or_mutable_source(tmp_path, platform, source):
    result, outputs = _plan(tmp_path, platform, source)
    assert result.returncode != 0
    assert outputs == {}


def test_binary_checks_have_deadlines_and_do_not_hide_command_failures() -> None:
    job = _workflow("binary-verify.yml")["jobs"]["verify"]
    assert 0 < job["timeout-minutes"] <= 60
    assert job["defaults"]["run"]["shell"] == "bash"
    for step in job["steps"]:
        assert 0 < step["timeout-minutes"] <= 15
        assert "continue-on-error" not in step
        if " | tee " in step.get("run", ""):
            assert "set -o pipefail" in step["run"]
    steps = {step["id"]: step for step in _steps() if "id" in step}
    assert "--report validation/smoke.json" in steps["smoke"]["run"]
    assert "pytest" in steps["install"]["run"]
    assert "python -m pytest tests/test_file_lock.py" in steps["file-locks"]["run"]
    for suite in ("test_process_identity.py", "test_instance_lock_safety.py",
                  "test_instance_lock_generation.py", "test_lock_domain.py"):
        assert suite in steps["file-locks"]["run"]
    assert "validation/file-locks.log" in steps["file-locks"]["run"]
    assert "if" not in steps["file-locks"]
    assert "tools/binary_manifest.py verify" in steps["manifest"]["run"]
    assert "--source-root ." in steps["manifest"]["run"]
    assert "if" not in steps["manifest"]
    outcomes = next(step for step in _steps() if step.get("name") == "Record verification outcomes")
    assert '"file-locks"' in outcomes["run"]
    assert steps["soak"]["if"] == "matrix.asset == 'linux-x86_64'"
    assert "--output validation/soak" in steps["soak"]["run"]
    assert steps["signals"]["if"] == "matrix.asset == 'linux-x86_64'"
    assert "tools/smoke-tui-signals.py --binary dist/pi-batch-linux-x86_64" in steps["signals"]["run"]
    assert "--output-dir validation/signals" in steps["signals"]["run"]
    assert '"signals"' in outcomes["run"]


def test_linux_recovery_and_batch_shutdown_are_required_binary_checks():
    steps = {step["id"]: step for step in _steps() if "id" in step}
    outcomes = next(step for step in _steps() if step.get("name") == "Record verification outcomes")
    for name, tool in (("recovery", "smoke-tui-recovery.py"),
                       ("batch-signals", "smoke-batch-signals.py")):
        assert steps[name]["if"] == "matrix.asset == 'linux-x86_64'"
        assert f"tools/{tool} --binary dist/pi-batch-linux-x86_64" in steps[name]["run"]
        assert f"--output-dir validation/{name}" in steps[name]["run"]
        assert f'"{name}"' in outcomes["run"]


def test_failed_verification_evidence_cannot_become_a_release_asset() -> None:
    uploads = [step for step in _steps() if "upload-artifact@" in step.get("uses", "")]
    evidence = next(step for step in uploads if step["with"]["path"] == "validation/")
    binary = next(step for step in uploads if step != evidence)
    outcomes = next(step for step in _steps() if step.get("name") == "Record verification outcomes")
    assert evidence["if"] == outcomes["if"] == "always()"
    assert binary.get("if", "success()") == "success()"
    jobs = _workflow("release.yml")["jobs"]
    download = jobs["release-assets"]["steps"][0]["with"]
    assert fnmatch.fnmatchcase(binary["with"]["name"], download["pattern"])
    assert not fnmatch.fnmatchcase(evidence["with"]["name"], download["pattern"])
    executable = "dist/pi-batch-${{ matrix.asset }}${{ matrix.extension }}"
    assert binary["with"]["path"].splitlines() == [
        executable, f"{executable}.manifest.json", f"{executable}.LICENSE"]


def test_publication_reuses_all_binary_checks_and_keeps_license_gate() -> None:
    jobs = _workflow("release.yml")["jobs"]
    assert jobs["build-binaries"] == {
        "name": "Verify platform binaries", "needs": "build-and-verify",
        "uses": "./.github/workflows/binary-verify.yml",
    }
    assert set(jobs["publish"]["needs"]) == {"build-and-verify", "build-binaries"}
    assert set(jobs["release-assets"]["needs"]) == {"build-and-verify", "build-binaries", "publish"}
    gates = "\n".join(step.get("run", "") for step in jobs["build-and-verify"]["steps"])
    assert 'tools/check-release.py --tag "${GITHUB_REF_NAME}"' in gates
