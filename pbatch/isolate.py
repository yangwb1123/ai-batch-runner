"""`pi-batch isolate` — worktree 隔离执行（多任务并行推进的标准模式）。

一个 SSH 会话 = 一个 worktree：在同一仓库上为每个任务建独立工作目录
+ 独立分支，各自跑 pi-batch（锁按 cwd 天然隔离、.pi-batch/ 会话/记忆
各自独立、agent 改代码互不干扰），任务完成后提示人工评审合并回主分支
（不自动合并——与 campaign --parallel-pipelines 同语义）。

用法：
    pi-batch isolate --branch task-a -- tasks-a.yaml --git-commit
    pi-batch isolate -b task-b -d ../proj-task-b -- tasks-b.yaml \
        --log-file logs/task-b.log --max-rounds 0
    pi-batch isolate -b task-c --copy-memory -- tasks-c.yaml

fail closed：非 git 仓库 / 分支已存在 / 目标目录非空 / 目标在仓库内 /
无任务参数 → 一律拒绝启动（exit 2），绝不静默降级。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import log

_LOCK_EXIT = 5


def _git(args: list, wd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(wd), *args],
                          capture_output=True, text=True)


def _repo_root(cwd: Path) -> Optional[Path]:
    """Resolve the actual worktree top-level for a caller in any subdirectory."""
    proc = _git(["rev-parse", "--show-toplevel"], cwd)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    root = Path(proc.stdout.strip()).resolve()
    return root if root.is_dir() else None


def _inside_repo(root: Path) -> bool:
    return _repo_root(root) is not None


def _branch_exists(root: Path, branch: str) -> bool:
    proc = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], root)
    return proc.returncode == 0


def _current_branch(root: Path) -> str:
    return _git(["branch", "--show-current"], root).stdout.strip()


def _worktree_dir(root: Path, branch: str, explicit: str) -> Path:
    """默认 ../<仓库名>-<branch>，显式 --dir 优先。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    name = root.name or "repo"
    return (root.parent / f"{name}-{branch}").resolve()


def _copy_memory(root: Path, worktree: Path) -> None:
    """延续主目录运行数据（会话/记忆/被拒产物）到新 worktree。

    src 用 config.data_dir（PBATCH_DATA_DIR 已设置时从外部目录复制）。"""
    from .config import data_dir
    src = data_dir(str(root))
    dst = worktree / ".pi-batch"
    if not src.is_dir():
        log.info("ISOLATE: no .pi-batch/ in %s to copy", root)
        return
    import shutil
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
        log.info("ISOLATE: copied .pi-batch/ from %s", root)
    except OSError as exc:
        log.warning("ISOLATE: could not copy .pi-batch/: %s", exc)


def _child_env(workdir: Path) -> dict:
    """Bind every child to its worktree-local runtime ledger."""
    env = os.environ.copy()
    env["PBATCH_DATA_DIR"] = str(workdir / ".pi-batch")
    return env


def run_isolated(root: Path, branch: str, workdir: Path, task_args: list,
                 copy_memory: bool = False) -> int:
    """建 worktree → 在隔离目录执行 pi-batch → 提示合并。返回子进程退出码。"""
    requested_root = root.resolve()
    root = _repo_root(requested_root)
    if root is None:
        log.error("ISOLATE: %s is not a git work tree", requested_root)
        return 2
    workdir = workdir.expanduser().resolve()
    if _branch_exists(root, branch):
        log.error("ISOLATE: branch '%s' already exists", branch)
        return 2
    if workdir.exists() and any(workdir.iterdir()):
        log.error("ISOLATE: target directory not empty: %s", workdir)
        return 2
    if root.resolve() in workdir.parents or workdir == root:
        log.error("ISOLATE: target %s must be outside the repository", workdir)
        return 2
    if not task_args:
        log.error("ISOLATE: no task arguments after '--' "
                  "(e.g. -- tasks.yaml --git-commit)")
        return 2

    base = _current_branch(root) or "HEAD"
    proc = _git(["worktree", "add", str(workdir), "-b", branch, base], root)
    if proc.returncode != 0:
        log.error("ISOLATE: git worktree add failed: %s", proc.stderr.strip())
        return 2
    log.info("ISOLATE: worktree %s (branch %s, from %s)", workdir, branch, base)
    if copy_memory:
        _copy_memory(root, workdir)

    # 子进程用同一 pi-batch 入口，cwd=worktree（锁按 cwd 天然隔离）。
    entry = sys.executable
    cmd = [entry, "-m", "pbatch", *task_args]
    log.info("ISOLATE: running: %s (cwd=%s)", " ".join(cmd), workdir)
    result = subprocess.run(cmd, cwd=str(workdir),
                            env=_child_env(workdir))
    log.info("ISOLATE: task exit=%d", result.returncode)
    if result.returncode == 0:
        print(f"\n[isolate] ALL PASSED on branch '{branch}'. Merge when ready:")
        print(f"  cd {root} && git merge {branch}")
        print(f"  git worktree remove {workdir}")
    else:
        code = "locked" if result.returncode == _LOCK_EXIT else "failed"
        print(f"\n[isolate] TASK {code.upper()} (exit={result.returncode}) — "
              f"worktree left in place for diagnosis:")
        print(f"  cd {workdir} && pi-batch ... --reuse   # 续跑")
        print(f"  git -C {workdir} status                 # 现场")
    return result.returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch.py isolate",
        description="Run pi-batch in an isolated git worktree (one SSH "
                    "session = one worktree; merge back manually).")
    parser.add_argument("-b", "--branch", required=True,
                        help="new branch for this task (must not exist)")
    parser.add_argument("-d", "--dir", default="",
                        help="worktree directory (default: <parent>/<repo>-<branch>)")
    parser.add_argument("--copy-memory", action="store_true",
                        help="copy .pi-batch/ (sessions/memory) from the main "
                             "worktree to continue context")
    parser.add_argument("task_args", nargs=argparse.REMAINDER,
                        help="args after '--' passed to pi-batch unchanged")
    return parser


def isolate_main(argv: list) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.task_args or args.task_args[0] != "--":
        parser.error("usage: pi-batch isolate -b <branch> [--dir DIR] -- "
                     "<pi-batch args...>")
    task_args = args.task_args[1:]
    cwd = Path.cwd().resolve()
    root = _repo_root(cwd)
    if root is None:
        log.error("ISOLATE: %s is not a git work tree", cwd)
        sys.exit(2)
    workdir = _worktree_dir(root, args.branch, args.dir)
    code = run_isolated(root, args.branch, workdir, task_args,
                        args.copy_memory)
    sys.exit(code)
