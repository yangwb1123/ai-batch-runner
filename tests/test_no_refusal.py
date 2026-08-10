"""Regression suite for the fail-closed refusal gate scripts/check-no-refusal.py.

Pins the gate's observable contract (exit 0/1/2, 10 markers, REFUSAL-CHECK
diagnostics) as black-box subprocess tests; enforces the single-canonical-copy
rule (deletion branch, design D3 — the example copy is gone and only the 8
pipeline validate_cmd strings may reference the script); asserts the
`refusalcheck` registry entry; and exercises the `pi-batch check` refusal
step (fail closed, never a no-op, unknown --steps names exit 2).

Deletion branch (design §8): `examples/snaplink-platform/scripts/
check-no-refusal.py` must NOT exist. The byte-identity fallback branch is NOT
live — exactly one branch (deletion XOR identity) is chosen.
"""

from __future__ import annotations

import re
import runpy
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-no-refusal.py"
EXAMPLE_COPY = ROOT / "examples" / "snaplink-platform" / "scripts" / "check-no-refusal.py"
PIPELINES = [
    ROOT / "examples" / "snaplink-platform" / "audit-revision-pipeline.yaml",
    ROOT / "examples" / "snaplink-platform" / "audit-fix-pipeline.yaml",
    ROOT / "examples" / "snaplink-platform" / "requirement-pipeline.yaml",
    ROOT / "examples" / "snaplink-platform" / "audit-v21-pipeline.yaml",
]
VALIDATE_CMD_RE = re.compile(
    r'^\s*validate_cmd:\s*"python3? \{cwd\}/scripts/check-no-refusal\.py \{output\}"\s*$')


def _gate(*paths) -> subprocess.CompletedProcess:
    """Run the gate against artifact paths; cwd = repo root (documented
    invocation mode, mirrors tests/test_entrypoints.py subprocess style)."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, paths)],
        cwd=ROOT, capture_output=True, text=True, timeout=120)


def _markers() -> list:
    """Marker list derived from the script source (auto-syncs with the list;
    safe under runpy.run_path — main() does not execute)."""
    return list(runpy.run_path(str(SCRIPT))["REFUSAL_MARKERS"])


# --- FR-1: gate contract (A1.x) -------------------------------------------

def test_clean_artifact_passes(tmp_path):
    """A1.4/A1.7: non-empty marker-free prose exits 0 with REFUSAL-CHECK: OK."""
    artifact = tmp_path / "clean.md"
    artifact.write_text("这是一份正常的阶段产物，内容完整。", encoding="utf-8")
    result = _gate(artifact)
    assert result.returncode == 0
    assert "REFUSAL-CHECK: OK" in result.stdout


@pytest.mark.parametrize("marker", _markers(), ids=lambda m: m[:12])
def test_marker_refuses(tmp_path, marker):
    """A1.1/A1.7: every marker in the script's list refuses the artifact."""
    artifact = tmp_path / "marked.md"
    artifact.write_text(f"这是一份正常的阶段产物，但其中包含标记：{marker}。",
                        encoding="utf-8")
    result = _gate(artifact)
    assert result.returncode == 1
    assert "REFUSAL-CHECK" in result.stdout
    assert "failing closed" in result.stdout


def test_marker_list_not_purged():
    """A1.6: the derived marker list never shrinks below the original 10
    (a purge would silently shrink A1.1's parametrization)."""
    assert len(_markers()) >= 10


def test_empty_output_refuses(tmp_path):
    """A1.2: empty artifact exits 1."""
    artifact = tmp_path / "empty.md"
    artifact.write_text("", encoding="utf-8")
    result = _gate(artifact)
    assert result.returncode == 1
    assert "REFUSAL-CHECK" in result.stdout
    assert "empty output" in result.stdout


def test_whitespace_only_refuses(tmp_path):
    """A1.2: whitespace-only artifact hits the strip() empty branch."""
    artifact = tmp_path / "ws.md"
    artifact.write_text("\n\t  \n", encoding="utf-8")
    result = _gate(artifact)
    assert result.returncode == 1
    assert "REFUSAL-CHECK" in result.stdout
    assert "empty output" in result.stdout


def test_missing_file_refuses(tmp_path):
    """A1.3: nonexistent artifact path exits 1."""
    result = _gate(tmp_path / "does-not-exist.md")
    assert result.returncode == 1
    assert "REFUSAL-CHECK" in result.stdout
    assert "artifact missing" in result.stdout


def test_usage_without_args():
    """A1.5: no-argument invocation exits 2 with usage text."""
    result = _gate()
    assert result.returncode == 2
    assert "usage:" in result.stdout


# --- FR-2: single canonical copy (A2.x) -----------------------------------

def test_no_duplicate_copy():
    """A2.1 (deletion branch): the drifting example copy is gone."""
    assert not EXAMPLE_COPY.exists()


def test_pipelines_reference_canonical_copy():
    """A2.2: exactly 8 validate_cmd refs across the 4 pipelines, each
    resolving to the canonical copy via {cwd}."""
    hits = []
    for path in PIPELINES:
        assert path.is_file(), f"pipeline missing: {path}"
        hits.extend(line for line in
                    path.read_text(encoding="utf-8").splitlines()
                    if "check-no-refusal" in line)
    assert len(hits) == 8, f"expected 8 refs, got {len(hits)}: {hits}"
    for line in hits:
        assert VALIDATE_CMD_RE.match(line), f"non-canonical ref: {line}"


def test_examples_tree_only_validate_cmd_refs():
    """A2.3: no stray check-no-refusal reference anywhere under examples/."""
    suffixes = {".yaml", ".yml", ".md", ".py", ".sh"}
    stray = []
    for path in (ROOT / "examples").rglob("*"):
        if path.is_file() and path.suffix in suffixes:
            for line in path.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
                if "check-no-refusal" in line and not VALIDATE_CMD_RE.match(line):
                    stray.append(f"{path}: {line}")
    assert not stray, "stray references:\n" + "\n".join(stray)


# --- FR-3: registry registration (A3.x) -----------------------------------

def test_registered_in_validators():
    """A3.1/A3.2: refusalcheck registered with the canonical script path
    (never a path under examples/)."""
    from pbatch import config as pbconfig
    entry = pbconfig.VALIDATORS.get("refusalcheck")
    assert entry, "refusalcheck missing from pi-batch.yaml validators"
    assert "scripts/check-no-refusal.py" in entry
    assert "examples/" not in entry


# --- FR-4: pi-batch check listing (A4.x) ----------------------------------

def test_refusal_step_in_default_steps():
    """FR-4 structural: the refusal step is part of the default check set."""
    from pbatch import selfcheck
    args = Namespace(skip_eval=True, quick_eval=False, steps="")
    names = [name for name, _ in selfcheck._default_steps(args)]
    assert "refusal" in names


def test_refusal_step_pass_with_live_config():
    """FR-4: the step passes against the live registry + canonical script."""
    from pbatch import selfcheck
    assert selfcheck._run_refusal_step() is True


def test_refusal_step_fails_closed_without_registry_entry():
    """A4.3: missing registry entry → False (fail closed, never a no-op)."""
    from pbatch import selfcheck
    assert selfcheck._run_refusal_step(validators={}, script=SCRIPT) is False


def test_refusal_step_fails_closed_without_script():
    """A4.3: missing canonical script → False."""
    from pbatch import selfcheck
    validators = {"refusalcheck":
                  "python {cwd}/scripts/check-no-refusal.py {output}"}
    assert selfcheck._run_refusal_step(validators=validators,
                                       script=ROOT / "scripts" / "nope.py") is False


def test_pi_batch_check_lists_gate():
    """A4.2: end-to-end — `pi-batch check --steps refusal` lists the gate."""
    result = subprocess.run(
        [sys.executable, "pi-batch.py", "check", "--steps", "refusal",
         "--skip-eval"],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-300:]
    assert "CHECK [refusal]" in result.stdout


def test_check_rejects_unknown_step():
    """D2/FIND-2: unknown --steps name exits 2 instead of silently passing
    0 gates (the no-op trap)."""
    result = subprocess.run(
        [sys.executable, "pi-batch.py", "check", "--steps", "not_a_real_step",
         "--skip-eval"],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 2
    assert "unknown step" in result.stderr


# --- DOC-1: AGENTS.md count truthful (A5.1) -------------------------------

def test_agents_md_validator_count_matches():
    """A5.1: the AGENTS.md validator count equals the live registry count
    (permanently guards FIND-1 instead of fixing it once)."""
    import yaml
    config = yaml.safe_load((ROOT / "pi-batch.yaml").read_text(encoding="utf-8"))
    count = len(config["validators"])
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    match = re.search(r"(\d+) 个验证器", text)
    assert match, "AGENTS.md missing 'N 个验证器' line"
    assert int(match.group(1)) == count, (
        f"AGENTS.md says {match.group(1)} validators, registry has {count}")
