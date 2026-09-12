"""Small status and admission helpers for campaign pipelines."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .pipeline_status import PipelineStatus


def classify_pipeline(results: list, failed: list[str], pipeline) -> tuple[str, str]:
    if not failed and all(result.success for result in results):
        return PipelineStatus.PASSED, ""
    failed_set = set(failed)
    if any(stage.gate and stage.name in failed_set for stage in pipeline.stages):
        return PipelineStatus.GATE_REJECTED, "gate verdict was not PASS"
    if any(result.validation_ok is False for result in results):
        return PipelineStatus.VALIDATION_FAILED, "engineering validator rejected an artifact"
    return PipelineStatus.PIPELINE_FAILED, ", ".join(failed) or "one or more tasks failed"


def parallel_ready(root: Path) -> tuple[bool, str]:
    """Parallel implementation requires a clean Git worktree at campaign start."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not verify clean Git worktree: {exc}"
    if proc.returncode != 0:
        return False, "parallel pipelines require a Git repository"
    if proc.stdout.strip():
        return False, "parallel pipelines require a clean worktree before analysis"
    return True, ""
