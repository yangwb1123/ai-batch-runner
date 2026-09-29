"""Deliverable evidence from an isolated campaign pipeline run.

An isolated direction pipeline runs in a child process, so the campaign
coordinator never sees its stage results and cannot reuse the in-process
``_artifact_evidence`` helper. The child's deliverables live in
``artifacts/<stage>/`` and are moved into a timestamped ``archive/`` directory
once the pipeline completes, so the ``VERDICT: PASS`` artifact the auto-merge
gate requires was unreachable and every passed isolated direction was rejected
with "no bounded VERDICT: PASS artifact supplied by the pipeline" - nothing was
ever merged.
"""

from __future__ import annotations

from pathlib import Path

MAX_EVIDENCE = 64
_PATTERNS = ("artifacts/**/*.md", "archive/*/*.md",
             "artifacts/**/*.txt", "archive/*/*.txt")


def isolated_run_evidence(pipeline_path: Path, limit: int = MAX_EVIDENCE) -> list[str]:
    """Return the isolated child's deliverable artifacts, bounded and ordered.

    Deterministic ordering keeps merge evidence stable between runs, symlinks
    and reuse sidecars are skipped, and ``limit`` caps the list so a chatty
    pipeline cannot blow up the merge record.
    """
    run_dir = pipeline_path.parent
    found: list[str] = []
    for pattern in _PATTERNS:
        for path in sorted(run_dir.glob(pattern)):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.endswith(".meta.json"):
                continue
            resolved = str(path)
            if resolved not in found:
                found.append(resolved)
            if len(found) >= max(1, limit):
                return found
    return found
