"""Release automation must preserve the public package and command contract."""

from __future__ import annotations

import re
import runpy
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "tools" / "check-release.py"
BINARY_BUILDER = ROOT / "tools" / "build-binary.py"
RELEASE_ERRORS = runpy.run_path(str(CHECK))["release_errors"]
BINARY_MODULE = runpy.run_path(str(BINARY_BUILDER))
ACTION_PINS = {
    "actions/checkout": (
        "d23441a48e516b6c34aea4fa41551a30e30af803", "v6.1.0"
    ),
    "actions/setup-python": (
        "ece7cb06caefa5fff74198d8649806c4678c61a1", "v6.3.0"
    ),
    "actions/upload-artifact": (
        "330a01c490aca151604b8cf639adc76d48f6c5d4", "v5.0.0"
    ),
    "actions/download-artifact": (
        "018cc2cf5baa6db3ef3c5f8a56943fffe632ef53", "v6.0.0"
    ),
    "pypa/gh-action-pypi-publish": (
        "dc37677b2e1c63e2034f94d8a5b11f265b73ba33", "v1.14.2"
    ),
}


def _version() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise AssertionError("project version not found")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), *args], cwd=ROOT,
        capture_output=True, text=True,
    )


def _release_fixture(root: Path, license_text: str) -> None:
    (root / "pyproject.toml").write_text(
        """[project]
name = "ai-batch-runner"
version = "1.2.3"

[project.scripts]
pi-batch = "pbatch.cli:main"
""",
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.2.3] - 2026-08-23\n", encoding="utf-8"
    )
    (root / "LICENSE").write_text(license_text, encoding="utf-8")


def test_release_metadata_is_consistent() -> None:
    version = _version()
    result = _run()
    assert result.returncode == 0, result.stderr
    assert f"ai-batch-runner {version} -> pi-batch" in result.stdout


def test_tagged_release_fails_closed_for_unresolved_license(tmp_path: Path) -> None:
    _release_fixture(
        tmp_path,
        "Upstream license must be re-checked for compatibility before further distribution.",
    )
    errors = RELEASE_ERRORS("v1.2.3", tmp_path)
    assert errors == [
        "tagged publication blocked: upstream snaplink/ai-dev license "
        "clearance is unresolved in LICENSE"
    ]


def test_tagged_release_can_succeed_after_license_clearance(tmp_path: Path) -> None:
    _release_fixture(tmp_path, "Copyright holders and redistribution terms verified.\n")
    assert RELEASE_ERRORS("v1.2.3", tmp_path) == []


def test_release_check_rejects_mismatched_tag() -> None:
    result = _run("--tag", "v9.9.9")
    assert result.returncode == 1
    assert f"does not match project version v{_version()}" in result.stderr


def test_release_check_does_not_accept_entry_point_text_in_a_comment(
        tmp_path: Path) -> None:
    _release_fixture(tmp_path, "Verified redistribution terms.\n")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'pi-batch = "pbatch.cli:main"',
            '# pi-batch = "pbatch.cli:main"\nwrong = "pbatch.cli:main"',
        ),
        encoding="utf-8",
    )

    errors = RELEASE_ERRORS("v1.2.3", tmp_path)

    assert any("console entry point pi-batch" in error for error in errors)


def test_binary_builder_contains_platform_assets_without_source_checkout_at_runtime() -> None:
    args = BINARY_MODULE["_add_data_args"](ROOT)
    joined = " ".join(args)
    assert "share/ai-batch-runner" in joined
    assert "share/ai-batch-runner/scripts" in joined
    assert "share/ai-batch-runner/ui-specs" in joined
    assert "share/ai-batch-runner/pbatch" in joined
    assert any(
        str(ROOT / "pbatch" / "selfcheck.py") in value for value in args)
    assert not any("__pycache__" in value for value in args)
    assert BINARY_MODULE["binary_label"]()


def test_binary_builder_embeds_utf8_for_redirected_multilingual_output(tmp_path, monkeypatch):
    commands = []
    def build_stub(command, **kwargs):
        commands.append(command)
        (tmp_path / "pi-batch.exe").write_bytes(b"frozen executable")
    monkeypatch.setattr(BINARY_MODULE["platform"], "system", lambda: "Windows")
    monkeypatch.setattr(BINARY_MODULE["subprocess"], "run", build_stub)
    executable = BINARY_MODULE["build"](tmp_path)
    command = commands[0]
    assert command[command.index("--python-option") + 1] == "X utf8=1"
    assert executable.name == "pi-batch.exe"


def test_release_workflow_builds_and_publishes_platform_binaries() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    assert "build-binaries:" in workflow
    assert "release-assets:" in workflow
    assert "uses: ./.github/workflows/binary-verify.yml" in workflow
    assert "gh release upload" in workflow
    binary = (ROOT / ".github/workflows/binary-verify.yml").read_text(encoding="utf-8")
    assert '.[binary]' in binary
    for target in ("linux-x86_64", "macos-x86_64", "macos-arm64", "windows-x86_64"):
        assert target in binary


def test_release_workflow_uses_oidc_and_a_protected_environment() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    assert "tags:" in workflow and "v[0-9]+.[0-9]+.[0-9]+" in workflow
    assert "environment:" in workflow and "name: pypi" in workflow
    assert "id-token: write" in workflow
    assert "password:" not in workflow and "api-token" not in workflow
    assert 'git merge-base --is-ancestor "${GITHUB_SHA}" origin/main' in workflow


def test_release_workflow_pins_every_action_to_a_commit() -> None:
    workflow = "\n".join(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        for name in ("release.yml", "binary-verify.yml"))
    raw_uses = re.findall(r"^\s*-?\s*uses:\s+(.+?)\s*$", workflow, re.M)
    uses = []
    for raw in raw_uses:
        if raw == "./.github/workflows/binary-verify.yml":
            assert (ROOT / raw).is_file()
            continue
        match = re.fullmatch(
            r"([^@\s]+)@([0-9a-f]{40})\s+#\s+(\S+)", raw)
        assert match is not None, f"action is not pinned to a full SHA: {raw}"
        uses.append(match.groups())
    assert {action: (sha, version) for action, sha, version in uses} == ACTION_PINS


def test_release_checker_is_not_runtime_package_data() -> None:
    assert CHECK.parent.name == "tools"
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    package_config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "tools/check-release.py" not in manifest
    assert '"share/ai-batch-runner/tools"' not in package_config
