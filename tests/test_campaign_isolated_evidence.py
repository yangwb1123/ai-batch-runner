"""Isolated campaign runs: deliverable evidence for the auto-merge gate.

Live finding (iris-grid-core-24h-v4, 2026-09-28): an isolated direction
pipeline runs in a child process, so the coordinator never sees its stage
results. Evidence was therefore limited to the worktree, the pipeline file,
and the run log, and because the child archives its deliverables on success
the `VERDICT: PASS` artifact the merge gate requires was unreachable — every
passed direction was rejected with "no bounded VERDICT: PASS artifact
supplied by the pipeline" and nothing was ever merged.
"""

from __future__ import annotations

from pathlib import Path

from pbatch.campaign_artifacts import isolated_run_evidence


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / "direction-1"
    (run_dir / "artifacts" / "implement-abc123").mkdir(parents=True)
    archive = run_dir / "archive" / "pipeline-20260928-073402"
    archive.mkdir(parents=True)
    (run_dir / "pipeline.yaml").write_text("stages: []\n", encoding="utf-8")
    (run_dir / "campaign.log").write_text("PIPELINE START\n", encoding="utf-8")
    return run_dir


def test_collects_archived_and_live_deliverables(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    live = run_dir / "artifacts" / "implement-abc123" / "task-1-implementation.md"
    live.write_text("draft\n", encoding="utf-8")
    archived = (run_dir / "archive" / "pipeline-20260928-073402" /
                "task-1-implementation.md")
    archived.write_text("VERDICT: PASS - focused tests green\n", encoding="utf-8")
    (run_dir / "archive" / "pipeline-20260928-073402" /
     "task-1-design.md").write_text("design\n", encoding="utf-8")

    evidence = isolated_run_evidence(run_dir / "pipeline.yaml")

    assert str(live) in evidence
    assert str(archived) in evidence
    assert str(run_dir / "archive" / "pipeline-20260928-073402" /
               "task-1-design.md") in evidence
    # The merge gate reads the verdict artifact, not the pipeline/log stubs.
    assert any(path.endswith("task-1-implementation.md")
               and "VERDICT: PASS" in Path(path).read_text(encoding="utf-8")
               for path in evidence)


def test_skips_meta_sidecars_and_is_deterministic_and_bounded(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    stage = run_dir / "artifacts" / "implement-abc123"
    (stage / "task-1-implementation.md.meta.json").write_text("{}", encoding="utf-8")
    (stage / "task-1-implementation.md").write_text("ok\n", encoding="utf-8")
    for index in range(5):
        (stage / f"task-{index + 2}-note.md").write_text("n\n", encoding="utf-8")

    first = isolated_run_evidence(run_dir / "pipeline.yaml")
    second = isolated_run_evidence(run_dir / "pipeline.yaml")

    assert first == second
    assert not any(path.endswith(".meta.json") for path in first)
    assert len(first) == 6
    assert len(isolated_run_evidence(run_dir / "pipeline.yaml", limit=3)) == 3


def test_missing_run_dir_is_not_an_error(tmp_path: Path) -> None:
    assert isolated_run_evidence(tmp_path / "absent" / "pipeline.yaml") == []
