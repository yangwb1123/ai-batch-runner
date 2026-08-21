"""Regression tests for `pi-batch isolate` — worktree-isolated execution
(one SSH session = one worktree; merge back to the main branch manually).

Covers: worktree creation + branch isolation, subprocess cwd (lock by
cwd), fail-closed preflight (non-git / existing branch / non-empty dir /
inside-repo target / missing task args), success/failure merge hints.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path):
    """A git repository with one committed file on main."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"],
                   check=True)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"],
                   check=True)
    return root


def _isolate(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "isolate",
         *args],
        capture_output=True, text=True, cwd=str(root), timeout=120)


def test_isolate_creates_worktree_and_runs(repo):
    """worktree 创建 + 分支隔离 + 子进程在隔离目录执行（--dry-run 验证）。"""
    result = _isolate(repo, "-b", "task-a", "--",
                      "-p", "test", "--dry-run")
    assert result.returncode == 0, result.stderr
    worktree = repo.parent / "repo-task-a"
    assert worktree.is_dir()
    # worktree 有自己的 .git 与分支
    assert (worktree / ".git").exists()
    branch = subprocess.run(
        ["git", "-C", str(worktree), "branch", "--show-current"],
        capture_output=True, text=True).stdout.strip()
    assert branch == "task-a"
    # 主分支未被污染
    main_branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True, text=True).stdout.strip()
    assert main_branch == "master" or main_branch == "main"
    # worktree 继承基线文件
    assert (worktree / "a.txt").is_file()


def test_isolate_locks_are_per_worktree(repo):
    """锁按 cwd：主目录持锁时隔离 worktree 仍可执行（隔离的意义）。"""
    lock = repo / ".pi-batch.lock"
    lock.write_text('{"pid": 999999999, "host": "x", "argv": []}', encoding="utf-8")
    result = _isolate(repo, "-b", "task-b", "--",
                      "-p", "test", "--dry-run")
    assert result.returncode == 0, result.stderr
    # worktree 内无锁文件（各自目录，天然隔离）
    assert not (repo.parent / "repo-task-b" / ".pi-batch.lock").exists()


def test_isolate_rejects_existing_branch(repo):
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "dup"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-"],
                   check=True)
    result = _isolate(repo, "-b", "dup", "--", "-p", "test", "--dry-run")
    assert result.returncode == 2
    assert "already exists" in result.stderr


def test_isolate_rejects_non_empty_target(repo):
    target = repo.parent / "occupied"
    target.mkdir()
    (target / "x.txt").write_text("x", encoding="utf-8")
    result = _isolate(repo, "-b", "task-c", "-d", str(target), "--",
                      "-p", "test", "--dry-run")
    assert result.returncode == 2
    assert "not empty" in result.stderr


def test_isolate_rejects_inside_repo_target(repo):
    target = repo / "inside"
    result = _isolate(repo, "-b", "task-d", "-d", str(target), "--",
                      "-p", "test", "--dry-run")
    assert result.returncode == 2
    assert "outside the repository" in result.stderr


def test_isolate_from_nested_dir_uses_actual_repository_root(repo):
    """子目录启动时，默认 worktree 仍必须在真实 Git 顶层之外。"""
    nested = repo / "nested"
    nested.mkdir()

    result = _isolate(nested, "-b", "task-nested", "--",
                      "-p", "test", "--dry-run")

    assert result.returncode == 0, result.stderr
    expected = repo.parent / "repo-task-nested"
    assert expected.is_dir()
    assert not (repo / "nested-task-nested").exists()
    branch = subprocess.run(
        ["git", "-C", str(expected), "branch", "--show-current"],
        capture_output=True, text=True).stdout.strip()
    assert branch == "task-nested"


def test_run_isolated_rejects_target_inside_true_repo_from_nested_cwd(repo):
    """run_isolated 的 API 边界也不得把子目录当成仓库根。"""
    from pbatch import isolate

    nested = repo / "nested"
    nested.mkdir()
    target = repo / "nested-task"

    assert isolate.run_isolated(
        nested, "task-nested-api", target,
        ["-p", "test", "--dry-run"]) == 2
    assert not target.exists()
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         "refs/heads/task-nested-api"],
        capture_output=True).returncode != 0


def test_isolate_rejects_non_git(tmp_path):
    result = _isolate(tmp_path, "-b", "task-e", "--",
                      "--dry-run")
    assert result.returncode == 2
    assert "not a git work tree" in result.stderr


def test_isolate_rejects_missing_task_args(repo):
    result = _isolate(repo, "-b", "task-f")
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_isolate_success_prints_merge_hints(repo):
    """成功 → 提示合并命令（不自动合并——与 campaign 同语义）。"""
    result = _isolate(repo, "-b", "task-g", "--",
                      "-p", "test", "--dry-run")
    assert "ALL PASSED" in result.stdout
    assert f"git merge task-g" in result.stdout
    assert "git worktree remove" in result.stdout


def test_isolate_copy_memory(repo):
    """--copy-memory：主目录 .pi-batch/ 延续到 worktree。"""
    (repo / ".pi-batch").mkdir()
    (repo / ".pi-batch" / "memory").write_text("{}", encoding="utf-8")
    result = _isolate(repo, "-b", "task-h", "--copy-memory", "--",
                      "-p", "test", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert (repo.parent / "repo-task-h" / ".pi-batch" / "memory").is_file()


def test_isolate_copy_memory_uses_local_data_dir(repo, tmp_path, monkeypatch):
    """外置 PBATCH_DATA_DIR 时，复制快照仍是子进程的活动目录。"""
    from pbatch import isolate

    external = tmp_path / "external-data"
    (external / "memory").mkdir(parents=True)
    (external / "memory" / "marker").write_text("snapshot\n", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(external))
    seen = {}
    real_run = isolate.subprocess.run

    def capture_child_env(cmd, **kwargs):
        if len(cmd) >= 3 and cmd[0] == sys.executable and cmd[1:3] == ["-m", "pbatch"]:
            seen["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(isolate.subprocess, "run", capture_child_env)
    worktree = repo.parent / "repo-task-copy-env"
    assert isolate.run_isolated(repo, "task-copy-env", worktree, ["--dry-run"], True) == 0
    assert (worktree / ".pi-batch" / "memory" / "marker").is_file()
    assert seen["env"]["PBATCH_DATA_DIR"] == str(worktree / ".pi-batch")


def test_isolate_without_copy_does_not_inherit_external_data_dir(
        repo, tmp_path, monkeypatch):
    """未复制历史时，外置父 ledger 也不得被多个 worktree 共享。"""
    from pbatch import isolate

    external = tmp_path / "shared-parent-data"
    external.mkdir()
    (external / "marker").write_text("parent only\n", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(external))
    seen = {}
    real_run = isolate.subprocess.run

    def capture_child_env(cmd, **kwargs):
        if len(cmd) >= 3 and cmd[0] == sys.executable and cmd[1:3] == ["-m", "pbatch"]:
            seen["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(isolate.subprocess, "run", capture_child_env)
    worktree = repo.parent / "repo-task-local-env"
    assert isolate.run_isolated(
        repo, "task-local-env", worktree, ["--dry-run"], False) == 0
    assert seen["env"]["PBATCH_DATA_DIR"] == str(worktree / ".pi-batch")
    assert seen["env"]["PBATCH_DATA_DIR"] != str(external)
    assert not (worktree / ".pi-batch" / "marker").exists()


def test_copy_memory_queries_remain_visible_across_worktree_scope(
        repo, tmp_path, monkeypatch):
    """复制后的 recent/find/read 使用同一 Git project scope，且边界仍关闭。"""
    from pbatch import config, isolate, memory, session
    from pbatch.memory_policy import memory_scope

    monkeypatch.delenv("PBATCH_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", "")
    monkeypatch.setenv("PBATCH_MEMORY_TENANT", "team-a")
    monkeypatch.setenv("PBATCH_MEMORY_SENSITIVITY", "confidential")
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path / "sessions")
    raw_dir = session.SESSIONS_DIR / session.workdir_key(str(repo))
    raw_dir.mkdir(parents=True)
    raw = raw_dir / "20260101_scope-session.jsonl"
    raw.write_text(json.dumps({
        "type": "message",
        "message": {"role": "assistant", "content": "remembered context"},
    }) + "\n", encoding="utf-8")
    memory.append_event({
        "type": "task", "session_id": "scope-session", "status": "PASSED",
        "prompt_excerpt": "MFA integration marker", "raw_session": str(raw),
        "cwd": str(repo),
    }, str(repo))
    parent_scope = memory_scope(str(repo))
    expected_id = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:24]
    assert parent_scope["project_id"] == expected_id

    seen = {}
    real_run = isolate.subprocess.run

    def capture_child_env(cmd, **kwargs):
        if len(cmd) >= 3 and cmd[0] == sys.executable and cmd[1:3] == ["-m", "pbatch"]:
            seen["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(isolate.subprocess, "run", capture_child_env)
    worktree = repo.parent / "repo-task-scoped-memory"
    assert isolate.run_isolated(
        repo, "task-scoped-memory", worktree, ["--dry-run"], True) == 0
    monkeypatch.setenv("PBATCH_DATA_DIR", seen["env"]["PBATCH_DATA_DIR"])

    assert memory_scope(str(worktree)) == parent_scope
    assert memory.recent(str(worktree), 1)[0]["session_id"] == "scope-session"
    assert memory.find("MFA integration", str(worktree), 1)[0]["status"] == "PASSED"
    read = memory.read_session("scope-session", str(worktree))
    assert "remembered context" in json.dumps(read["messages"])

    monkeypatch.setenv("PBATCH_MEMORY_SENSITIVITY", "secret")
    assert memory.recent(str(worktree), 1) == []
    assert memory.find("MFA integration", str(worktree), 1) == []
    assert memory.read_session("scope-session", str(worktree))["missing"] is True


# ── data-dir：运行状态移到项目外（不污染项目代码结构）───────────────

def test_data_dir_env_moves_runtime_state(tmp_path, monkeypatch):
    """PBATCH_DATA_DIR：记忆索引/被拒产物移到外部；锁仍按 cwd。"""
    from pbatch import config
    from pbatch.triage import write_cmd_failure_diagnostics

    external = tmp_path / "data" / "pbatch"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(external))
    assert config.data_dir(str(tmp_path)) == external.resolve()
    # 被拒诊断 → 外部 data 目录（不污染项目）
    write_cmd_failure_diagnostics("cmd", 7, "boom", str(tmp_path))
    rejected = external / "rejected"
    assert list(rejected.glob("verify-cmd-*.txt"))
    assert not (tmp_path / ".pi-batch").exists()


def test_data_dir_default_is_project_local(tmp_path, monkeypatch):
    """默认（无 env/CLI）：data_dir = cwd/.pi-batch（兼容现状）。"""
    from pbatch import config
    monkeypatch.delenv("PBATCH_DATA_DIR", raising=False)
    config.set_data_dir_override("")
    assert config.data_dir(str(tmp_path)) == (tmp_path / ".pi-batch").resolve()


def test_data_dir_cli_flag_runs_clean(tmp_path):
    """--data-dir 端到端：任务运行不产生项目内 .pi-batch 污染。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   check=True)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"],
                   check=True)
    external = tmp_path / "data"
    agent = tmp_path / "fake-agent.sh"
    agent.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    agent.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "-p", "test",
         "-o", "docs/result.md", "--agent-bin", str(agent),
         "--data-dir", str(external), "--no-lock"],
        capture_output=True, text=True, cwd=str(repo), timeout=60)
    assert result.returncode == 0, result.stderr
    # 运行状态 → 外部 data 目录；项目内零 .pi-batch 污染
    assert external.exists()
    assert not (repo / ".pi-batch").exists()
    assert (external / "memory").exists() or (repo / "docs" / "result.md").is_file()


def test_memory_index_follows_data_dir(tmp_path, monkeypatch):
    """记忆索引默认路径随 data_dir。"""
    from pbatch import config
    from pbatch.memory import index_path
    external = tmp_path / "external"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(external))
    path = index_path(str(tmp_path))
    assert str(path).startswith(str(external.resolve()))
    assert path.name == "sessions.index.jsonl"
