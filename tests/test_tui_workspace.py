"""Read-only, bounded workspace review contracts for the TUI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pbatch.tui_backend import TUIBackend
from pbatch.tui_workspace import (capture_workspace, workspace_delta,
                                  workspace_diff, workspace_diff_scoped)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        check=True, timeout=10,
    )


def _repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "tui@example.test")
    _git(tmp_path, "config", "user.name", "TUI Test")
    (tmp_path / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "baseline")
    return tmp_path


def test_capture_reports_staged_unstaged_untracked_and_ignores_runtime(tmp_path):
    root = _repository(tmp_path)
    (root / "tracked.txt").write_text("after\n", encoding="utf-8")
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    (root / ".pi-batch").mkdir()
    (root / ".pi-batch" / "state").write_text("runtime\n", encoding="utf-8")
    _git(root, "add", "staged.txt")

    status = capture_workspace(str(root)).details()

    assert status["available"] is True and status["branch"] == "main"
    assert status["changed"] == 3
    assert status["staged"] == 1
    assert status["unstaged"] == 1
    assert status["untracked"] == 1
    assert {item["path"] for item in status["files"]} == {
        "tracked.txt", "staged.txt", "untracked.txt",
    }


def test_diff_includes_tracked_staged_and_untracked_content(tmp_path):
    root = _repository(tmp_path)
    (root / "tracked.txt").write_text("tracked change\n", encoding="utf-8")
    (root / "staged.txt").write_text("staged content\n", encoding="utf-8")
    (root / "untracked.txt").write_text("untracked content\n", encoding="utf-8")
    _git(root, "add", "staged.txt")

    result = workspace_diff(str(root))

    assert result["error"] == ""
    assert "tracked change" in result["patch"]
    assert "staged content" in result["patch"]
    assert "untracked content" in result["patch"]


def test_scoped_diff_separates_index_worktree_and_untracked(tmp_path):
    root = _repository(tmp_path)
    (root / "tracked.txt").write_text("worktree change\n", encoding="utf-8")
    (root / "staged.txt").write_text("staged content\n", encoding="utf-8")
    (root / "untracked.txt").write_text("untracked content\n", encoding="utf-8")
    _git(root, "add", "staged.txt")

    staged = workspace_diff_scoped(str(root), scope="staged")
    unstaged = workspace_diff_scoped(str(root), scope="unstaged")
    untracked = workspace_diff_scoped(str(root), scope="untracked")

    assert "staged content" in staged["patch"]
    assert "worktree change" not in staged["patch"]
    assert [item["path"] for item in staged["files"]] == ["staged.txt"]
    assert "worktree change" in unstaged["patch"]
    assert "staged content" not in unstaged["patch"]
    assert [item["path"] for item in unstaged["files"]] == ["tracked.txt"]
    assert "untracked content" in untracked["patch"]
    assert [item["path"] for item in untracked["files"]] == ["untracked.txt"]


def test_base_and_commit_scopes_resolve_ref_and_enumerate_files(tmp_path):
    root = _repository(tmp_path)
    baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
    (root / "tracked.txt").write_text("committed change\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "second")

    base = workspace_diff_scoped(str(root), scope="base", target=baseline)
    commit = workspace_diff_scoped(str(root), scope="commit", target="HEAD")

    assert base["error"] == "" and base["resolved_target"] == baseline
    assert "committed change" in base["patch"]
    assert [item["path"] for item in base["files"]] == ["tracked.txt"]
    assert commit["error"] == "" and len(commit["resolved_target"]) == 40
    assert "committed change" in commit["patch"]
    assert [item["path"] for item in commit["files"]] == ["tracked.txt"]


def test_target_scope_rejects_missing_or_option_like_ref(tmp_path):
    root = _repository(tmp_path)

    missing = workspace_diff_scoped(str(root), scope="base", target="missing")
    option = workspace_diff_scoped(str(root), scope="commit", target="--all")

    assert missing["patch"] == "" and "unable to resolve Git ref missing" in missing["error"]
    assert option["patch"] == "" and "unsupported characters" in option["error"]


def test_diff_path_is_scoped_to_workspace(tmp_path):
    root = _repository(tmp_path)

    result = workspace_diff(str(root), "../outside.txt")

    assert result["patch"] == ""
    assert "inside the Git workspace" in result["error"]


def test_diff_in_unborn_repository_includes_staged_content(tmp_path):
    _git(tmp_path, "init", "--initial-branch=main")
    target = tmp_path / "first.txt"
    target.write_text("first revision\n", encoding="utf-8")
    _git(tmp_path, "add", "first.txt")

    result = workspace_diff(str(tmp_path), "first.txt")

    assert result["error"] == ""
    assert "first revision" in result["patch"]


def test_delta_detects_further_edit_with_same_porcelain_status(tmp_path):
    root = _repository(tmp_path)
    target = root / "tracked.txt"
    target.write_text("first edit\n", encoding="utf-8")
    before = capture_workspace(str(root))
    target.write_text("second edit\n", encoding="utf-8")
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    delta = workspace_delta(before, capture_workspace(str(root)))

    assert delta["changed"] is True
    assert delta["paths"] == ["tracked.txt"]


def test_backend_exposes_post_turn_workspace_delta(tmp_path):
    root = _repository(tmp_path)
    agent = root / "agent.sh"
    agent.write_text(
        "#!/bin/sh\nprintf 'agent edit\\n' > tracked.txt\nprintf 'done\\n'\n",
        encoding="utf-8",
    )
    agent.chmod(0o755)
    _git(root, "add", "agent.sh")
    _git(root, "commit", "-m", "agent")
    backend = TUIBackend(cwd=str(root), agent=str(agent), memory_mode="off",
                         session_id="workspace-session")

    result = backend.run_turn("edit the file")
    snapshot = backend.snapshot()

    assert result.success
    assert snapshot["workspace"]["changed"] == 1
    assert snapshot["last_turn_workspace"]["changed"] is True
    assert snapshot["last_turn_workspace"]["paths"] == ["tracked.txt"]
    assert backend.workspace_diff("tracked.txt")["error"] == ""
