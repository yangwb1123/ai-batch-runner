"""Parallel collaboration coordinator contracts."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from pbatch.collab import collab_main


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    (root / "b.txt").write_text("b\n", encoding="utf-8")
    _git(root, "add", "a.txt", "b.txt")
    _git(root, "-c", "user.name=tests", "-c",
         "user.email=tests@example.invalid", "commit", "-qm", "base")
    return root


def _plan(path: Path, worktree_root: Path) -> None:
    path.write_text(
        "workspace: " + str(path.parent / "repo") + "\n"
        "worktree_root: " + str(worktree_root) + "\n"
        "workers: 2\n"
        "agents:\n"
        "  - id: core\n"
        "    seed_dirty: [a.txt]\n"
        "    write_scope: [a.txt]\n"
        "    prompt: implement core\n"
        "    timeout: 30\n"
        "  - id: adapter\n"
        "    seed_dirty: [b.txt]\n"
        "    write_scope: [b.txt]\n"
        "    prompt: implement adapter\n"
        "    timeout: 30\n",
        encoding="utf-8")


def test_collab_dry_run_has_no_side_effects(tmp_path, capsys):
    root = _repo(tmp_path)
    plan = tmp_path / "plan.yaml"
    worktree_root = tmp_path / "collab-worktrees"
    _plan(plan, worktree_root)

    assert collab_main(["--root", str(root), "--plan", str(plan), "--dry-run"]) == 0
    assert not worktree_root.exists()
    assert "core" in capsys.readouterr().out


def test_collab_runs_disjoint_agents_in_external_worktrees(tmp_path):
    root = _repo(tmp_path)
    (root / "a.txt").write_text("dirty-a\n", encoding="utf-8")
    (root / "b.txt").write_text("dirty-b\n", encoding="utf-8")
    fake = tmp_path / "fake-agent"
    fake.write_text("#!/bin/sh\nprintf 'agent result\\n'\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    plan = tmp_path / "plan.yaml"
    worktree_root = tmp_path / "collab-worktrees"
    _plan(plan, worktree_root)

    result = collab_main([
        "--root", str(root), "--plan", str(plan),
        "--agent-bin", str(fake),
    ])

    assert result == 0
    assert (worktree_root / "core" / "a.txt").read_text(encoding="utf-8") == "dirty-a\n"
    assert (worktree_root / "adapter" / "b.txt").read_text(encoding="utf-8") == "dirty-b\n"
    assert (root / "a.txt").read_text(encoding="utf-8") == "dirty-a\n"
    assert (root / "b.txt").read_text(encoding="utf-8") == "dirty-b\n"
    assert list(worktree_root.glob(".collab-*/*/*.patch"))


def test_collab_rejects_overlapping_write_scopes(tmp_path):
    root = _repo(tmp_path)
    plan = tmp_path / "overlap.yaml"
    plan.write_text(
        "workspace: " + str(root) + "\n"
        "agents:\n"
        "  - id: one\n    write_scope: [packages/**]\n    prompt: one\n"
        "  - id: two\n    write_scope: [packages/core/**]\n    prompt: two\n",
        encoding="utf-8")

    assert collab_main(["--root", str(root), "--plan", str(plan)]) == 2
