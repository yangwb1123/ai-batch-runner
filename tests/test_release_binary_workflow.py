"""Linux publication binds validated tags, verified artifacts and immutable versions."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
TAG = "v1.2.3"


def _workflow(name="release-binary.yml"):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def _step(job, name):
    return next(step for step in _workflow()["jobs"][job]["steps"]
                if name in step.get("name", ""))


def _shell(script, cwd, **env):
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script], cwd=cwd,
        env={**os.environ, **env}, capture_output=True, text=True, timeout=10)


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def release_source(tmp_path):
    origin, checkout = tmp_path / "origin", tmp_path / "checkout"
    origin.mkdir()
    _git(origin, "init", "--quiet", "-b", "main")
    _git(origin, "config", "user.name", "Workflow Test")
    _git(origin, "config", "user.email", "workflow@example.invalid")
    _git(origin, "commit", "--quiet", "--allow-empty", "-m", "release source")
    _git(origin, "tag", "-a", TAG, "-m", "annotated release")
    _git(tmp_path, "clone", "--quiet", str(origin), str(checkout))
    _git(checkout, "checkout", "--quiet", "--detach", TAG)
    return origin, checkout, _git(checkout, "rev-parse", "HEAD")


def _fake_gh(tmp_path):
    executable = tmp_path / "gh"
    executable.write_text(
        f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
        'Path(os.environ["GH_RECORD"]).write_text(json.dumps(sys.argv[1:]))\n'
        'sys.exit(int(os.environ.get("GH_RESULT", "0")))\n')
    executable.chmod(0o755)
    return {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "GH_RECORD": str(tmp_path / "gh-record.json")}


def test_linux_release_is_explicit_and_independent_of_pypi():
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["tag"]["required"] is True
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "release-linux-refs/tags/${{ inputs.tag }}", "cancel-in-progress": False}
    full_group = _workflow("release.yml")["concurrency"]["group"].replace(
        "${{ github.ref }}", "refs/tags/" + TAG)
    linux_group = workflow["concurrency"]["group"].replace("${{ inputs.tag }}", TAG)
    assert linux_group != full_group  # A pending PyPI approval cannot queue the Linux run.
    jobs = workflow["jobs"]
    assert jobs["build-binary"] == {
        "name": "Verify the Linux release binary", "needs": "validate",
        "uses": "./.github/workflows/binary-verify.yml",
        "with": {"platform": "linux-x86_64",
                 "source_sha": "${{ needs.validate.outputs.source_sha }}"}}
    assert set(jobs["publish"]["needs"]) == {"validate", "build-binary"}
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    assert jobs["publish"]["environment"]["name"] == "binary-release"
    for name, job in jobs.items():
        if name != "publish":
            assert "permissions" not in job
        for step in job.get("steps", []):
            assert "continue-on-error" not in step and "if" not in step
            assert 0 < step["timeout-minutes"] <= 30
            assert "pypi" not in step.get("uses", "").lower()
            assert "${{" not in step.get("run", "")


@pytest.mark.parametrize("tag,ref,allowed", [
    (TAG, "refs/heads/main", True), ("main", "refs/heads/main", False),
    (TAG + "\nsource_sha=other", "refs/heads/main", False),
    ("v1.2.3; touch unexpected", "refs/heads/main", False),
    (TAG, "refs/heads/feature", False),
])
def test_release_input_validation_rejects_implicit_or_injected_refs(tmp_path, tag, ref, allowed):
    result = _shell(_step("validate", "Validate explicit")["run"], tmp_path,
                    RELEASE_TAG=tag, GITHUB_REF=ref)
    assert (result.returncode == 0) is allowed, result.stderr


def test_tag_resolution_uses_annotated_commit_and_main_ancestry(release_source, tmp_path):
    origin, checkout, commit = release_source
    output = tmp_path / "outputs"
    step = _step("validate", "Resolve tag")
    result = _shell(step["run"], checkout, RELEASE_TAG=TAG, GITHUB_OUTPUT=str(output))
    assert result.returncode == 0, result.stderr
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
        "tag": TAG, "source_sha": commit}
    _git(origin, "checkout", "--quiet", "-b", "unmerged")
    _git(origin, "commit", "--quiet", "--allow-empty", "-m", "not on main")
    _git(origin, "tag", "--force", TAG)
    _git(checkout, "fetch", "--quiet", "--force", "origin", f"refs/tags/{TAG}:refs/tags/{TAG}")
    _git(checkout, "checkout", "--quiet", "--detach", TAG)
    refused = tmp_path / "refused-outputs"
    result = _shell(step["run"], checkout, RELEASE_TAG=TAG, GITHUB_OUTPUT=str(refused))
    assert result.returncode != 0 and not refused.exists()


def test_linux_release_keeps_license_gate_and_pins_build_and_publish_source():
    jobs = _workflow()["jobs"]
    for job in ("validate", "publish"):
        script = "\n".join(step.get("run", "") for step in jobs[job]["steps"])
        assert 'python tools/check-release.py --tag "$RELEASE_TAG"' in script
    assert _step("validate", "Run engineering")["run"] == "make ci"
    checkout = next(step for step in jobs["publish"]["steps"]
                    if "checkout@" in step.get("uses", ""))
    assert checkout["with"]["ref"] == "${{ needs.validate.outputs.source_sha }}"
    assert checkout["with"]["persist-credentials"] is False
    download = _step("publish", "Download this run")
    assert download["with"] == {"name": "pi-batch-binary-linux-x86_64", "path": "dist/"}
    verify = _step("publish", "Verify exact source")["run"]
    assert "tools/binary_manifest.py verify" in verify and "--source-root ." in verify
    assert 'test "$(git rev-parse HEAD)" = "$SOURCE_SHA"' in verify


@pytest.mark.parametrize("commit,dirty,allowed", [
    ("a" * 40, False, True), ("b" * 40, False, False),
    ("a" * 40, True, False), ("a" * 40, None, False),
])
def test_publish_provenance_requires_exact_clean_source(tmp_path, commit, dirty, allowed):
    directory = tmp_path / "dist"
    directory.mkdir()
    (directory / "pi-batch-linux-x86_64.manifest.json").write_text(json.dumps({
        "source": {"git": {"commit": commit, "dirty": dirty}}}))
    script = _step("publish", "Verify exact source")["run"]
    verifier = script.split("python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    result = subprocess.run(
        [sys.executable, "-c", verifier], cwd=tmp_path,
        env={**os.environ, "SOURCE_SHA": "a" * 40}, capture_output=True, text=True,
        timeout=10)
    assert (result.returncode == 0) is allowed, result.stderr


@pytest.mark.parametrize("moved", [False, True])
def test_publish_rechecks_remote_tag_before_any_github_write(release_source, tmp_path, moved):
    origin, checkout, commit = release_source
    env = _fake_gh(tmp_path)
    if moved:
        _git(origin, "commit", "--quiet", "--allow-empty", "-m", "tag moved")
        _git(origin, "tag", "--force", TAG)
    result = _shell(_step("publish", "Recheck remote")["run"], checkout,
                    RELEASE_TAG=TAG, SOURCE_SHA=commit, **env)
    record = Path(env["GH_RECORD"])
    if moved:
        assert result.returncode != 0 and not record.exists()
        return
    assert result.returncode == 0, result.stderr
    command = json.loads(record.read_text())
    assert command[:3] == ["release", "create", TAG]
    assert command[command.index("--target") + 1] == commit
    assert command[3:7] == ["dist/pi-batch-linux-x86_64",
                            "dist/pi-batch-linux-x86_64.manifest.json",
                            "dist/pi-batch-linux-x86_64.LICENSE", "dist/SHA256SUMS"]
    assert "--verify-tag" in command and "--clobber" not in command


def test_release_entries_never_overwrite_an_existing_version(release_source, tmp_path):
    _, checkout, commit = release_source
    env = _fake_gh(tmp_path)
    result = _shell(_step("publish", "Recheck remote")["run"], checkout,
                    RELEASE_TAG=TAG, SOURCE_SHA=commit, GH_RESULT="42", **env)
    assert result.returncode == 42
    for name in ("release.yml", "release-binary.yml"):
        text = (ROOT / ".github/workflows" / name).read_text()
        assert "--clobber" not in text and "gh release upload" not in text


def test_full_release_checksums_use_downloaded_asset_basenames(tmp_path):
    directory = tmp_path / "dist"
    directory.mkdir()
    assets = ["pi-batch-linux-x86_64", "pi-batch-linux-x86_64.manifest.json",
              "pi-batch-linux-x86_64.LICENSE"]
    for name in assets:
        (directory / name).write_bytes(b"verified artifact")
    step = _workflow("release.yml")["jobs"]["release-assets"]["steps"][-1]
    result = _shell(step["run"], tmp_path, GITHUB_REF_NAME=TAG,
                    GITHUB_SHA="a" * 40, **_fake_gh(tmp_path))
    assert result.returncode == 0, result.stderr
    checksum_names = [line.split()[1] for line in (directory / "SHA256SUMS").read_text().splitlines()]
    assert set(checksum_names) == set(assets)
    command = json.loads((tmp_path / "gh-record.json").read_text())
    assert set(assets).issubset(command) and "SHA256SUMS" in command
