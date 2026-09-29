"""Bounded feedback extraction and reinjection for campaign retries."""

from __future__ import annotations

from pathlib import Path

from .text_io import read_text_bounded

_FEEDBACK_MAX_BYTES = 24 * 1024
_FEEDBACK_MAX_FILES = 6


def prior_gate_feedback(run_dir: Path) -> str:
    """Collect bounded gate verdicts and nearby reason lines from earlier attempts."""
    if not run_dir.is_dir():
        return ""
    blocks: list[str] = []
    total = 0
    for path in sorted(run_dir.rglob("*.md")):
        if not _is_feedback_file(path):
            continue
        try:
            text = read_text_bounded(path, _FEEDBACK_MAX_BYTES, "prior gate feedback")
        except (OSError, ValueError):
            continue
        for chunk in _verdict_blocks(text):
            if len(chunk) + total > _FEEDBACK_MAX_BYTES:
                break
            if len(blocks) >= _FEEDBACK_MAX_FILES:
                return "\n\n".join(blocks)
            blocks.append(chunk)
            total += len(chunk)
    return "\n\n".join(blocks)


def inject_prior_gate_feedback(stages: list, run_dir: Path) -> None:
    """Append prior gate evidence to prompts for the next implementation attempt."""
    feedback = prior_gate_feedback(run_dir)
    if not feedback:
        return
    note = (
        "\n\nPREVIOUS ATTEMPT FEEDBACK — an earlier run of this direction was "
        "rejected. Verify and address EVERY finding below with evidence (code, "
        "tests, or an explicit disposition); the gate will re-check them:\n"
        + feedback
    )
    for stage in stages:
        if stage.get("name") in ("design", "implement"):
            for task in stage.get("tasks", []):
                if task.get("prompt"):
                    task["prompt"] += note
        if stage.get("from_prompt"):
            stage["from_prompt"] += note


def _is_feedback_file(path: Path) -> bool:
    return any(key in path.name for key in (
        "gate", "verdict", "acceptance", "decision"))


def _verdict_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    blocks = []
    for index, line in enumerate(lines):
        if "VERDICT" not in line.upper():
            continue
        selected = [line.strip()]
        for follow in lines[index + 1:index + 26]:
            stripped = follow.strip()
            if not stripped:
                break
            selected.append(stripped[:300])
        blocks.append("\n".join(selected))
    return blocks
