"""Packaging regression suite: pbatch data files must ship in wheel and sdist.

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

Fix (design M1): one `[tool.setuptools.package-data]` block keyed to the import
name `pbatch` with an explicit file list (design gate condition: no globs, so a
new data file fails closed until consciously declared).

Regression discipline (from the design gate's binding conditions and reviews):
- Builds always run from a minimal tmp_path copy via a subprocess with
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

Deferred surface (documented, owners recorded — design gate condition 5):
pip installs still resolve out-of-package assets by design, deferred to
follow-up campaign directions: `{tool_root}` validators + `rule_matcher`
ui-specs/backend-specs registries + `examples/` default pipelines (owner:
direction #2, CI packaging gate / support-matrix decision U1), version
identity/tagging (direction #3), health probe for data-file presence (M3).
This suite is the compensating control: any loader data file referenced via
`with_name(...)` must ship, or the build-based tests fail closed.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
PBATCH = REPO_ROOT / "pbatch"

# The three data files that must ship inside the pbatch package.
EXPECTED_DATA_FILES = ("role_keywords.yaml", "task_keywords.yaml", "README.md")
SDIST_PREFIX = "ai-batch-runner-0.1.0/"

_WITH_NAME_RE = re.compile(r'with_name\(\s*"([^"]+\.(?:yaml|md))"\s*\)')
_BUILD_SDIST_SCRIPT = (
    "from setuptools import build_meta; import sys; "
    "print(build_meta.build_sdist(sys.argv[1]))"
)
_BUILD_WHEEL_SCRIPT = (
    "from setuptools import build_meta; import sys; "
    "print(build_meta.build_wheel(sys.argv[1]))"
)


def _loader_data_refs() -> set[str]:
    """Data files referenced by loaders via Path(__file__).with_name(...)."""
    refs: set[str] = set()
    for py in sorted(PBATCH.glob("*.py")):
        refs.update(_WITH_NAME_RE.findall(py.read_text(encoding="utf-8")))
    return refs


def _minimal_repo_copy(tmp_path: Path) -> Path:
    """Copy only what setuptools needs; exclude everything tree-state-ish."""
    copy = tmp_path / "proj"
    copy.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy(REPO_ROOT / name, copy / name)
    shutil.copytree(
        PBATCH, copy / "pbatch",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
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
    for name in EXPECTED_DATA_FILES:
        assert f"{SDIST_PREFIX}pbatch/{name}" in names, (
            f"{name} missing from sdist: {sorted(names)}"
        )


def test_loader_referenced_data_files_ship(sdist_file: Path) -> None:
    """Code-arch F1 invariant: every with_name('*.yaml'/'*.md') ref must ship."""
    refs = _loader_data_refs()
    assert refs, "loader-audit regex found no with_name data refs - audit broken"
    names = set(_tar_names(sdist_file))
    for ref in sorted(refs):
        assert f"{SDIST_PREFIX}pbatch/{ref}" in names, (
            f"{ref} referenced by a loader but not packaged - declare it in "
            "pyproject.toml [tool.setuptools.package-data]"
        )


def test_sdist_data_files_byte_identical_to_repo(sdist_file: Path) -> None:
    """AC-3 restated: installed YAML must be byte-identical to the repo copy."""
    with tarfile.open(sdist_file) as tar:
        for name in EXPECTED_DATA_FILES:
            member = f"{SDIST_PREFIX}pbatch/{name}"
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
    names = set(_tar_names(_build_sdist(copy, out)))
    for name in EXPECTED_DATA_FILES:
        assert f"{SDIST_PREFIX}pbatch/{name}" not in names, (
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
    """AC-1 (wheel half): in-process build, skipped when `wheel` is absent.

    The `wheel` package is not installed in this dev environment; `make ci`
    runs the sdist proxy instead (same manifest machinery). Adding `wheel` to
    the dev extras is the CI-visibility follow-up (campaign direction #2).
    """
    pytest.importorskip("wheel")
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
        for name in EXPECTED_DATA_FILES:
            assert f"pbatch/{name}" in names, (
                f"{name} missing from wheel {wheel_name}"
            )
