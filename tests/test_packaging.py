"""Packaging regression suite: runtime assets must ship in wheel and sdist.

Campaign direction: package-pbatch-data-files-role-keywords-yaml (task-1).
Evidence chain: docs/auto/runs/package-pbatch-data-files-role-keywords-yaml-tas-8807d80f/
(requirements-10762e10, design-a77de8a6, design_gate-6a76b0dd).

Bug under test: pyproject.toml declared `[tool.setuptools.packages.find]` only
(no package-data, no MANIFEST.in), so `pip install .` / pipx / uv installs
silently lost `pbatch/role_keywords.yaml` (meta role registry, 34 roles),
`pbatch/task_keywords.yaml` (classifier keywords) and `pbatch/README.md`.
`relevance.py:51` and `classifier.py:136` resolve them via
`Path(__file__).with_name(...)`; on a broken install relevance degrades to `{}`
(fail-open) and the classifier silently merges only built-in defaults.

The distribution has two complementary asset boundaries:
- `[tool.setuptools.package-data]` explicitly lists files loaded beside Python
  modules. A new loader resource fails closed until consciously declared.
- `[tool.setuptools.data-files]` installs repository-level validators, specs,
  prompts, examples and configuration below `share/ai-batch-runner`.
  `MANIFEST.in` carries those sources through an sdist-to-wheel build.

Regression discipline (from the design gate's binding conditions and reviews):
- Builds always run from a declared-input tmp_path copy via a subprocess with
  `cwd=copy` — never from the live tree. `setuptools.build_meta.build_sdist`
  builds from the *current working directory* (the `sdist_directory` argument
  is only the output dir), so building in-process from the repo root silently
  pulls in tree state (tests/, stale egg-info/SOURCES.txt) and false-passes.
- Assertions use exact member names (`pbatch/role_keywords.yaml in names`),
  never slash-substrings and never member counts (tree-state dependent).
- The wheel test skips when the `wheel` package is absent (the sdist test is
  the deterministic offline gate; same manifest machinery). `wheel` is a dev
  extra follow-up (campaign direction #2).
- AC-2's fresh-venv smoke (`cd /tmp`, PyYAML pre-installed) and AC-4's exact
  sdist script are manual acceptance steps; the sdist assertions here are the
  automated proxy.

The full runtime asset surface is now declared as setuptools
``data-files``: `{tool_root}` validators, rule registries, default pipelines,
role prompts, evals and governance specs are available after a wheel install.
The suite remains the compensating control: a newly referenced or required
asset must be added deliberately, or the build-based tests fail closed.
The fixture copies exactly the package tree plus every declared data-file
source. This keeps builds isolated without manufacturing the old false green
where sdist creation tolerated absent shared assets and wheel creation failed
later while copying them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PBATCH = REPO_ROOT / "pbatch"

# The three data files that must ship inside the pbatch package.
EXPECTED_DATA_FILES = ("role_keywords.yaml", "task_keywords.yaml", "README.md")
EXPECTED_RUNTIME_ASSETS = (
    "examples/llm-pool.yaml", "docs/LLM_ACCOUNT_POOL.md",
    "pi-batch.yaml", "quality.py", "scripts/check-ui-spec.py",
    "scripts/check-frontend-quality.py", "scripts/check-evidence.py",
    "scripts/check-completion-report.py", "ui-specs/rules.yaml",
    "backend-specs/rules.yaml", "product-specs/rules.yaml",
    "evolution-specs/rules.yaml", "examples/frontend-implementation-pipeline.yaml",
    "examples/agent-hub-gateway-oauth.json",
    "examples/agent-hub-user-auth.json",
    "examples/sdlc-inputs/oidc-logout-frontchannel.md",
    "prompts/architect.md", "agent-engineering/requirement.yaml",
)

_WITH_NAME_RE = re.compile(r'with_name\(\s*"([^"]+\.(?:yaml|md))"\s*\)')
_DATA_FILES_SECTION_RE = re.compile(
    r"\[tool\.setuptools\.data-files\](.*?)(?=\n\[|\Z)", re.S,
)
_DATA_FILES_ENTRY_RE = re.compile(
    r'^"([^"]+)"\s*=\s*\[(.*?)\]\s*$', re.M | re.S,
)
_TOML_STRING_RE = re.compile(r'"([^"]+)"')
_BUILD_SDIST_SCRIPT = (
    "from setuptools import build_meta; import sys; "
    "print(build_meta.build_sdist(sys.argv[1]))"
)
_BUILD_WHEEL_SCRIPT = (
    "from setuptools import build_meta; import sys; "
    "print(build_meta.build_wheel(sys.argv[1]))"
)
_INSTALLED_RUNTIME_PROBE = r"""
from pathlib import Path
import subprocess

from pbatch import assets, config, health
from pbatch.cmd_expand import expand_cmd

asset_root = Path(assets.TOOL_ROOT)
assert asset_root.as_posix().endswith("share/ai-batch-runner"), asset_root
assert health._asset_status()["healthy"]
config_text = (asset_root / "pi-batch.yaml").read_text(encoding="utf-8")
assert "/home/u1" not in config_text
assert (asset_root / "examples/sdlc-inputs/oidc-logout-frontchannel.md").is_file()
assert not (Path.cwd() / "scripts").exists()

artifacts = {
    "evidence": Path("evidence.md"),
    "uispacing": Path("clean.tsx"),
    "completion_go": Path("completion.md"),
}
for name, artifact in artifacts.items():
    entry = config.VALIDATORS[name]
    command = entry if isinstance(entry, str) else entry["cmd"]
    command = expand_cmd(command, artifact.resolve(), str(Path.cwd().resolve()))
    assert str(asset_root / "scripts") in command, (name, command)
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    assert result.returncode == 0, (name, command, result.stdout, result.stderr)
"""


def _loader_data_refs() -> set[str]:
    """Data files referenced by loaders via Path(__file__).with_name(...)."""
    refs: set[str] = set()
    for py in sorted(PBATCH.glob("*.py")):
        refs.update(_WITH_NAME_RE.findall(py.read_text(encoding="utf-8")))
    return refs


def _declared_data_file_patterns() -> dict[str, tuple[str, ...]]:
    """Read the simple string-list data-files table without Python 3.11."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = _DATA_FILES_SECTION_RE.search(text)
    assert section, "missing [tool.setuptools.data-files] table"
    entries = {
        destination: tuple(_TOML_STRING_RE.findall(values))
        for destination, values in _DATA_FILES_ENTRY_RE.findall(section.group(1))
    }
    assert entries, "[tool.setuptools.data-files] has no entries"
    assert all(entries.values()), "data-files destinations require source patterns"
    return entries


def _declared_data_file_sources() -> list[tuple[str, Path]]:
    """Expand every declared source glob and reject empty declarations."""
    result = []
    for destination, patterns in _declared_data_file_patterns().items():
        for pattern in patterns:
            matches = sorted(path for path in REPO_ROOT.glob(pattern)
                             if path.is_file())
            assert matches, f"data-files pattern matches nothing: {pattern}"
            result.extend((destination, path) for path in matches)
    return result


def _copy_declared_data_files(copy: Path) -> None:
    """Copy data-file sources while preserving their repository paths."""
    for _, source in _declared_data_file_sources():
        target = copy / source.relative_to(REPO_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _minimal_repo_copy(tmp_path: Path) -> Path:
    """Copy declared build inputs; exclude unrelated live-tree state."""
    copy = tmp_path / "proj"
    copy.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        shutil.copy(REPO_ROOT / name, copy / name)
    shutil.copytree(
        PBATCH, copy / "pbatch",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    _copy_declared_data_files(copy)
    return copy


def _build_sdist(copy: Path, out_dir: Path) -> Path:
    """Build the sdist in a subprocess with cwd=copy (never the live tree)."""
    result = subprocess.run(
        [sys.executable, "-c", _BUILD_SDIST_SCRIPT, str(out_dir)],
        cwd=copy, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    name = result.stdout.strip().splitlines()[-1]
    return out_dir / name


def _tar_names(path: Path) -> list[str]:
    with tarfile.open(path) as tar:
        return tar.getnames()


def _sdist_prefix(path: Path) -> str:
    """Return the archive's single setuptools-generated root directory.

    Setuptools may normalize the distribution component with underscores or
    hyphens; member assertions care about the payload path, not that valid
    spelling choice.
    """
    roots = {name.split("/", 1)[0] for name in _tar_names(path) if name}
    assert len(roots) == 1, f"sdist must have one root directory: {sorted(roots)}"
    return next(iter(roots)) + "/"


@pytest.fixture(scope="module")
def sdist_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One sdist build per test session, from an isolated minimal copy."""
    copy = _minimal_repo_copy(tmp_path_factory.mktemp("sdist"))
    out = copy / "dist"
    out.mkdir()
    return _build_sdist(copy, out)


def test_sdist_ships_declared_data_files(sdist_file: Path) -> None:
    """AC-1 (sdist half): exact member names, never counts or substrings."""
    names = set(_tar_names(sdist_file))
    prefix = _sdist_prefix(sdist_file)
    for name in EXPECTED_DATA_FILES:
        assert f"{prefix}pbatch/{name}" in names, (
            f"{name} missing from sdist: {sorted(names)}"
        )


def test_sdist_ships_declared_shared_assets(sdist_file: Path) -> None:
    """Every wheel data-file source must first survive the sdist boundary."""
    names = set(_tar_names(sdist_file))
    prefix = _sdist_prefix(sdist_file)
    for _, source in _declared_data_file_sources():
        relative = source.relative_to(REPO_ROOT).as_posix()
        assert f"{prefix}{relative}" in names, (
            f"declared shared asset missing from sdist: {relative}"
        )


def test_org_assets_are_declared_for_both_artifacts() -> None:
    """Installed org must carry its node registry through sdist and wheel."""
    patterns = _declared_data_file_patterns()
    destination = "share/ai-batch-runner/agent-engineering"
    assert patterns.get(destination) == ("agent-engineering/*.yaml",)
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include agent-engineering *.yaml" in manifest.splitlines()


def test_loader_referenced_data_files_ship(sdist_file: Path) -> None:
    """Code-arch F1 invariant: every with_name('*.yaml'/'*.md') ref must ship."""
    refs = _loader_data_refs()
    assert refs, "loader-audit regex found no with_name data refs - audit broken"
    names = set(_tar_names(sdist_file))
    prefix = _sdist_prefix(sdist_file)
    for ref in sorted(refs):
        assert f"{prefix}pbatch/{ref}" in names, (
            f"{ref} referenced by a loader but not packaged - declare it in "
            "pyproject.toml [tool.setuptools.package-data]"
        )


def test_sdist_data_files_byte_identical_to_repo(sdist_file: Path) -> None:
    """AC-3 restated: installed YAML must be byte-identical to the repo copy."""
    prefix = _sdist_prefix(sdist_file)
    with tarfile.open(sdist_file) as tar:
        for name in EXPECTED_DATA_FILES:
            member = f"{prefix}pbatch/{name}"
            shipped = tar.extractfile(member)
            assert shipped is not None, f"{member} absent from sdist"
            assert shipped.read() == (PBATCH / name).read_bytes(), (
                f"{name} shipped content differs from the repo copy"
            )


def test_sdist_isolated_from_live_tree_state(sdist_file: Path) -> None:
    """QA-F3 / testing-reviewer condition: build must not leak tree state."""
    names = _tar_names(sdist_file)
    assert not any("/tests/" in n for n in names), (
        "sdist leaked tests/ from the live tree - build must use the "
        "minimal tmp_path copy (cwd bug)"
    )


def test_sdist_ships_runtime_assets(sdist_file: Path) -> None:
    """The install-outside-checkout contract includes tool-root assets."""
    names = set(_tar_names(sdist_file))
    prefix = _sdist_prefix(sdist_file)
    for relative in EXPECTED_RUNTIME_ASSETS:
        assert f"{prefix}{relative}" in names, (
            f"runtime asset missing from sdist: {relative}"
        )


def test_distributed_validator_commands_are_tool_rooted() -> None:
    """Bundled checker entrypoints must not depend on the target checkout."""
    config_path = REPO_ROOT / "pi-batch.yaml"
    config_text = config_path.read_text(encoding="utf-8")
    assert "/home/u1" not in config_text
    registry = yaml.safe_load(config_text)["validators"]
    for name, entry in registry.items():
        command = entry if isinstance(entry, str) else entry.get("cmd", "")
        if "scripts/" in command:
            assert "{tool_root}/scripts/" in command, (name, command)
            assert "{cwd}/scripts/" not in command, (name, command)


def test_wrong_package_data_key_fails_closed(tmp_path: Path) -> None:
    """QA-F2: underscore egg-info name (ai_batch_runner) is a plausible typo.

    A wrong key builds successfully with an empty data-file set; the suite
    must discriminate that (sdist test doubles as the proxy: both artifacts
    use the same manifest machinery).
    """
    copy = _minimal_repo_copy(tmp_path)
    pyproject = copy / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    rewritten = text.replace("pbatch = [", "ai_batch_runner = [", 1)
    assert rewritten != text, "package-data block not found to rewrite"
    pyproject.write_text(rewritten, encoding="utf-8")
    out = copy / "dist"
    out.mkdir()
    archive = _build_sdist(copy, out)
    names = set(_tar_names(archive))
    prefix = _sdist_prefix(archive)
    for name in EXPECTED_DATA_FILES:
        assert f"{prefix}pbatch/{name}" not in names, (
            f"wrong package-data key still shipped {name} - discrimination lost"
        )


def test_package_data_declaration_covers_loader_refs() -> None:
    """F1/F4 invariant, static half: declaration == loader references, no globs.

    The package-data block must be an explicit list (design gate condition 1);
    a glob ('*.yaml') would silently ship or silently miss future files.
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(
        r"\[tool\.setuptools\.package-data\](.*?)(?=\n\[|\Z)",
        pyproject, re.S,
    )
    assert match, "missing [tool.setuptools.package-data] block in pyproject.toml"
    block = match.group(1)
    assert "*" not in block, "package-data must be an explicit list, not a glob"
    declared = set(re.findall(r'"([^"]+\.(?:yaml|md))"', block))
    loader_refs = _loader_data_refs()
    assert loader_refs, "loader-audit regex found no with_name data refs"
    assert loader_refs <= declared, (
        f"loader-referenced files not declared in package-data: "
        f"{sorted(loader_refs - declared)}"
    )


def test_wheel_ships_data_files(tmp_path: Path) -> None:
    """Build, inspect and install a wheel in an isolated target directory."""
    import wheel  # noqa: F401  # dev extra makes this gate non-skippable
    copy = _minimal_repo_copy(tmp_path)
    out = copy / "dist"
    out.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", _BUILD_WHEEL_SCRIPT, str(out)],
        cwd=copy, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    wheel_name = result.stdout.strip().splitlines()[-1]
    with zipfile.ZipFile(out / wheel_name) as wheel:
        names = set(wheel.namelist())
        for module in ("pbatch/tui.py", "pbatch/tui_backend.py"):
            assert module in names, f"TUI module missing from wheel: {module}"
        metadata_members = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_members) == 1, metadata_members
        metadata = wheel.read(metadata_members[0]).decode("utf-8")
        textual_requirements = [
            line.partition(":")[2].strip()
            for line in metadata.splitlines()
            if line.lower().startswith("requires-dist: textual")
        ]
        assert len(textual_requirements) == 1, textual_requirements
        textual_requirement = textual_requirements[0].replace(" ", "").lower()
        assert ">=8.2" in textual_requirement
        assert "<9" in textual_requirement
        for name in EXPECTED_DATA_FILES:
            assert f"pbatch/{name}" in names, (
                f"{name} missing from wheel {wheel_name}"
            )
        runtime_prefixes = [
            name.split(".data/data/", 1)[0] + ".data/data/"
            for name in names
            if ".data/data/share/ai-batch-runner/" in name
        ]
        assert runtime_prefixes, "wheel has no installed runtime data-files"
        runtime_prefix = runtime_prefixes[0]
        for relative in EXPECTED_RUNTIME_ASSETS:
            assert f"{runtime_prefix}share/ai-batch-runner/{relative}" in names, (
                f"runtime asset missing from wheel: {relative}"
            )
        for destination, source in _declared_data_file_sources():
            suffix = f".data/data/{destination}/{source.name}"
            assert any(name.endswith(suffix) for name in names), (
                f"declared shared asset missing from wheel: "
                f"{destination}/{source.name}"
            )

    # Content-only checks are insufficient: verify the installed distribution
    # can discover and execute its validators without PBATCH_TOOL_ROOT, a
    # source checkout, or project-local copies of scripts/.
    target = tmp_path / "installed"
    installed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps",
         "--no-compile", "--target", str(target), str(out / wheel_name)],
        capture_output=True, text=True,
    )
    assert installed.returncode == 0, installed.stderr
    external = tmp_path / "external-project"
    external.mkdir()
    (external / "evidence.md").write_text(
        "Status remains [PROPOSED] until verification.\n", encoding="utf-8")
    (external / "clean.tsx").write_text(
        "SizedBox(height: 24)\nstyle={classes.x}\n", encoding="utf-8")
    (external / "completion.md").write_text(
        "```yaml\ncompletion_report:\n  summary: packaged validator smoke\n"
        "  changed_files: [pi-batch.yaml]\n  commands_executed:\n"
        "    - command: packaging-smoke\n      result: passed\n"
        "  residual_risks: []\n  assumptions: []\n```\n",
        encoding="utf-8",
    )
    probe_env = {**__import__("os").environ, "PYTHONPATH": str(target)}
    probe_env.pop("PBATCH_TOOL_ROOT", None)
    probe_env.pop("PBATCH_SCRIPT_DIR", None)
    probe = subprocess.run(
        [sys.executable, "-c", _INSTALLED_RUNTIME_PROBE],
        cwd=external, env=probe_env,
        capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr + probe.stdout
