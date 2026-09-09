from __future__ import annotations

import subprocess
from pathlib import Path

from pbatch.campaign_merge import review_and_merge
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


def _branch(root: Path, baseline: str, tmp_path: Path) -> tuple[Path, str]:
    worktree = tmp_path / "worktree"
    branch = "campaign/test-direction"
    _git(root, "worktree", "add", "-b", branch, str(worktree), baseline)
    (worktree / "change.txt").write_text("implemented\n", encoding="utf-8")
    (worktree / "acceptance.md").write_text(
        "VERDICT: PASS - focused tests and acceptance evidence passed\n",
        encoding="utf-8")
    _git(worktree, "add", "change.txt", "acceptance.md")
    _git(worktree, "commit", "-qm", "implement direction")
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
