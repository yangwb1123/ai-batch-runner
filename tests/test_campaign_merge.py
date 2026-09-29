from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from types import SimpleNamespace

from pbatch.campaign_merge import record_campaign_outcomes, review_and_merge
from pbatch.campaign_pipeline import PipelineOutcome
from pbatch.campaign_models import Direction
from pbatch.pipeline_status import PipelineStatus


def _git(path: Path, *args: str, check: bool = True):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                          text=True, check=check)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Campaign Test")
    (root / "source.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "source.txt")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD").stdout.strip()


def _branch(root: Path, baseline: str, tmp_path: Path,
            name: str = "test-direction") -> tuple[Path, str]:
    worktree = tmp_path / f"worktree-{name}"
    branch = f"campaign/{name}"
    _git(root, "worktree", "add", "-b", branch, str(worktree), baseline)
    (worktree / "change.txt").write_text("implemented\n", encoding="utf-8")
    (worktree / "acceptance.md").write_text(
        "VERDICT: PASS - focused tests and acceptance evidence passed\n",
        encoding="utf-8")
    _git(worktree, "add", "change.txt", "acceptance.md")
    # The direction's own commit runs the repository hooks; the test hook
    # installed by the hook-specific cases is created after this point.
    _git(worktree, "commit", "--no-verify", "-qm", "implement direction")
    return worktree, branch


def test_review_and_merge_requires_gate_evidence_and_merges(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    worktree, branch = _branch(root, baseline, tmp_path)
    result = review_and_merge(
        root, worktree, branch, baseline, "direction-1",
        "python3 -c 'print(\"gate passed\")'",
        [str(worktree / "acceptance.md")],
        evidence_dir=tmp_path / "merge-evidence")

    assert result.status == PipelineStatus.MERGED
    assert result.merge_commit
    assert (root / "change.txt").read_text(encoding="utf-8") == "implemented\n"
    assert not worktree.exists()
    assert _git(root, "show-ref", "--verify", "refs/heads/" + branch,
                 check=False).returncode != 0
    assert Path(result.evidence[0]).is_file()


def test_review_and_merge_blocks_dirty_root_without_touching_branch(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    worktree, branch = _branch(root, baseline, tmp_path)
    (root / "unrelated.txt").write_text("do not overwrite\n", encoding="utf-8")

    result = review_and_merge(
        root, worktree, branch, baseline, "direction-2",
        "true", [str(worktree / "acceptance.md")],
        evidence_dir=tmp_path / "merge-evidence")

    assert result.status == PipelineStatus.MERGE_BLOCKED
    assert "root is dirty" in result.reason
    assert worktree.exists()
    assert _git(root, "show-ref", "--verify", "refs/heads/" + branch,
                 check=False).returncode == 0
    assert not (root / "change.txt").exists()


def test_campaign_outcome_auto_merge_records_merge_evidence(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    worktree, branch = _branch(root, baseline, tmp_path)
    direction = Direction(module="src", title="Auto merge direction")
    outcome = PipelineOutcome(
        direction, PipelineStatus.PASSED, "", 1.0,
        [str(worktree), str(worktree / "acceptance.md")], branch,
        _git(worktree, "rev-parse", "HEAD").stdout.strip(), baseline,
        _git(worktree, "rev-parse", "HEAD").stdout.strip(), [])
    context = SimpleNamespace(
        root=root,
        args=argparse.Namespace(auto_merge=True, no_lock=True,
                                merge_gate="true", merge_squash=False,
                                merge_wait_lock=0, timeout=30))
    events = []
    record_campaign_outcomes(context, [outcome], {direction.direction_id: "fp"},
                             lambda *args: events.append(args))

    assert outcome.status == PipelineStatus.PASSED
    assert outcome.merge_status == PipelineStatus.MERGED
    assert outcome.merge_commit
    assert events[-1][-1]["merge_status"] == PipelineStatus.MERGED
    assert (root / "change.txt").exists()


def test_auto_merge_accepts_archived_isolated_verdict_artifact(tmp_path: Path) -> None:
    """An isolated run's verdict lives in the child's archive directory, so
    the coordinator evidence must be collected from there (see
    tests/test_campaign_isolated_evidence.py)."""
    from pbatch.campaign_artifacts import isolated_run_evidence

    root, baseline = _repo(tmp_path)
    worktree, branch = _branch(root, baseline, tmp_path)
    (worktree / "acceptance.md").unlink()
    _git(worktree, "commit", "-aqm", "drop in-tree acceptance artifact")
    run_dir = worktree / ".pi-batch" / "runs" / "direction-1"
    archive = run_dir / "archive" / "pipeline-20260928-073402"
    archive.mkdir(parents=True)
    (run_dir / "pipeline.yaml").write_text("stages: []\n", encoding="utf-8")
    (archive / "task-1-implementation.md").write_text(
        "VERDICT: PASS - 12/12 focused tests green\n", encoding="utf-8")
    evidence = [str(worktree), str(run_dir / "pipeline.yaml")]
    evidence.extend(isolated_run_evidence(run_dir / "pipeline.yaml"))

    result = review_and_merge(
        root, worktree, branch, baseline, "direction-1", "true", evidence,
        evidence_dir=tmp_path / "merge-evidence")

    assert result.status == PipelineStatus.MERGED, result.reason
    assert (root / "change.txt").read_text(encoding="utf-8") == "implemented\n"


def test_automatic_merge_ignores_repository_commit_hooks(tmp_path: Path) -> None:
    """The direction commit already ran the repository hooks in its worktree
    and the merge gate re-validates scope, the framework-free rule and the test
    suites, so a hook that re-lints every previously merged file must not block
    the mechanical squash commit."""
    root, baseline = _repo(tmp_path)
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook refuses' >&2\nexit 1\n",
                    encoding="utf-8")
    hook.chmod(0o755)
    worktree, branch = _branch(root, baseline, tmp_path)

    result = review_and_merge(
        root, worktree, branch, baseline, "direction-hooked", "true",
        [str(worktree / "acceptance.md")],
        evidence_dir=tmp_path / "merge-evidence")

    assert result.status == PipelineStatus.MERGED, result.reason
    assert (root / "change.txt").exists()


def test_abort_partial_merge_clears_a_staged_squash_result(tmp_path: Path) -> None:
    """Live case: `git merge --squash` staged the merged result and the
    following commit was rejected (repository pre-commit hook), so the
    campaign repository was left dirty and every later direction was refused
    with "repository root is dirty"."""
    from pbatch.campaign_merge import _abort_partial_merge

    root, _baseline = _repo(tmp_path)
    (root / "merged.txt").write_text("squashed result\n", encoding="utf-8")
    _git(root, "add", "merged.txt")
    assert _git(root, "status", "--porcelain").stdout.strip()

    _abort_partial_merge(root)

    assert _git(root, "status", "--porcelain").stdout == ""
    assert not (root / "merged.txt").exists()


def test_conflicting_direction_leaves_the_root_clean_for_the_next_direction(
        tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    worktree, branch = _branch(root, baseline, tmp_path)
    # Same file, incompatible content: the direction conflicts by construction.
    (root / "source.txt").write_text("campaign moved on\n", encoding="utf-8")
    _git(root, "commit", "-aqm", "campaign change")
    (worktree / "source.txt").write_text("direction rewrote the base\n",
                                         encoding="utf-8")
    _git(worktree, "commit", "-aqm", "direction change")

    conflicted = review_and_merge(
        root, worktree, branch, baseline, "direction-conflict", "true",
        [str(worktree / "acceptance.md")],
        evidence_dir=tmp_path / "merge-evidence")

    assert conflicted.status == PipelineStatus.MERGE_BLOCKED
    assert _git(root, "status", "--porcelain").stdout == ""
    assert (root / "source.txt").read_text(encoding="utf-8") == "campaign moved on\n"

    # A later, non-conflicting direction must still merge.
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    next_worktree, next_branch = _branch(root, head, tmp_path, "next-direction")
    (next_worktree / "other.txt").write_text("second direction\n", encoding="utf-8")
    _git(next_worktree, "add", "other.txt")
    _git(next_worktree, "commit", "-qm", "second direction")

    merged = review_and_merge(
        root, next_worktree, next_branch, head, "direction-next", "true",
        [str(next_worktree / "acceptance.md")],
        evidence_dir=tmp_path / "merge-evidence")

    assert merged.status == PipelineStatus.MERGED, merged.reason
    assert (root / "other.txt").read_text(encoding="utf-8") == "second direction\n"


def test_review_and_merge_blocks_without_pass_verdict(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    worktree, branch = _branch(root, baseline, tmp_path)
    evidence = worktree / "acceptance.md"
    evidence.write_text("VERDICT: FAIL - missing evidence\n", encoding="utf-8")
    _git(worktree, "add", "acceptance.md")
    _git(worktree, "commit", "-qm", "failed evidence")

    result = review_and_merge(
        root, worktree, branch, baseline, "direction-3", "true",
        [str(evidence)], evidence_dir=tmp_path / "merge-evidence")

    assert result.status == PipelineStatus.MERGE_BLOCKED
    assert "VERDICT: PASS" in result.reason
    assert worktree.exists()
    assert not (root / "change.txt").exists()
