"""`pi-batch isolate` — worktree 隔离执行（多任务并行 / session 驱动的标准模式）。

一个 SSH 会话 = 一个 worktree：在同一仓库上为每个任务建独立工作目录
+ 独立分支，各自跑 pi-batch（锁按 workspace runtime 隔离、用户级会话/记忆
各自独立、agent 改代码互不干扰）。

两种收尾模式：

- 默认（manual）：任务完成后提示人工评审合并回主分支（不自动合并——与
  campaign --parallel-pipelines 同语义）。
- `--auto-merge`（session 模式）：任务成功后由本进程充当 Merge Controller，
  在根仓库锁内执行「fetch → rebase 最新主分支 → 验证门禁 → 合并」（实现
  见 pbatch/session.py），全部通过才自动合并主分支并清理 worktree/分支；
  任一环节失败保留现场。

用法：
    pi-batch isolate --branch task-a -- tasks-a.yaml --git-commit
    pi-batch isolate -b task-b -d ../proj-task-b -- tasks-b.yaml \
        --log-file logs/task-b.log --max-rounds 0
    pi-batch isolate -b task-c --copy-memory -- tasks-c.yaml
    # session 模式：验证通过后自动合并；Makefile 有 ci target 时默认门禁为 make ci
    pi-batch isolate -b session/20260821-091300 --auto-merge -- tasks.yaml
    pi-batch isolate -b fix-x --auto-merge --gate "npm run lint && npm test" -- tasks.yaml
    pi-batch isolate -b core-a -d /tmp/iris-core-a \
        --seed-dirty packages/core/src \
        --write-scope packages/core/src \
        --patch-file /tmp/iris-core-a.patch -- core-task.yaml
    pi-batch isolate sessions [--limit 20] [--json]   # session 状态账本

`--seed-dirty` is the protected dirty-slice mode: it copies only the
selected current dirty paths into an external worktree, forbids commits, and
rejects any later write outside `--write-scope` (which defaults to the seed
paths). Run one such command per module to get real parallel agents; the
source worktree remains a read-only baseline and the patch is for review only.

fail closed：非 git 仓库 / 分支已存在 / 目标目录非空 / 目标在仓库内 /
无任务参数 → 一律拒绝启动（exit 2），绝不静默降级。

session 模式退出码（状态账本见 pbatch/session.py）：
    0  任务通过且已合并（或被合并状态为无变更）
    2  前置校验失败 / 非 git
    5  合并队列忙（根仓库锁被其它 session/实例持有，--wait-minutes 可排队）
    6  合并被阻止：worktree 脏 / rebase 冲突 / 门禁失败 / 未检测到门禁
    （其余为子任务退出码：任务失败时保留 worktree 现场）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import data_dir, log
from .user_dirs import project_runtime_dir
from .lock import acquire_lock, release_lock
from .session_merge import (MERGE_BLOCKED_EXIT, SessionOptions, append_session,
                      merge_session_block, read_sessions, session_id)
from .worktree_slice import (git_head, normalize_patterns, scope_snapshot,
                             scope_violations, seed_dirty_slice, worktree_digest,
                             write_patch)

_LOCK_EXIT = 5
SCOPE_BLOCKED_EXIT = 6


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
    src = data_dir(str(root))
    dst = project_runtime_dir(str(worktree))
    if not src.is_dir():
        log.info("ISOLATE: no runtime data in %s to copy", src)
        return
    import shutil
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
        log.info("ISOLATE: copied runtime data from %s", src)
    except OSError as exc:
        log.warning("ISOLATE: could not copy runtime data: %s", exc)


def _child_env(workdir: Path) -> dict:
    """Bind every child to its private worktree runtime ledger."""
    env = os.environ.copy()
    env["PBATCH_DATA_DIR"] = str(project_runtime_dir(str(workdir)))
    return env


def _preflight(root: Path, branch: str, workdir: Path, task_args: list,
               sess: str, protected: bool = False,
               patch_file: str = "") -> Optional[int]:
    """fail-closed 前置检查；违规时记录 ABORTED 并返回退出码，通过返回 None。"""
    if _branch_exists(root, branch):
        append_session(root, sess, "ABORTED",
                       reason=f"branch {branch} already exists")
        log.error("ISOLATE: branch '%s' already exists", branch)
        return 2
    if workdir.exists() and any(workdir.iterdir()):
        append_session(root, sess, "ABORTED",
                       reason=f"target directory not empty: {workdir}")
        log.error("ISOLATE: target directory not empty: %s", workdir)
        return 2
    if root.resolve() in workdir.parents or workdir == root:
        append_session(root, sess, "ABORTED",
                       reason=f"target inside repository: {workdir}")
        log.error("ISOLATE: target %s must be outside the repository", workdir)
        return 2
    if not task_args:
        append_session(root, sess, "ABORTED",
                       reason="no task arguments after '--'")
        log.error("ISOLATE: no task arguments after '--' "
                  "(e.g. -- tasks.yaml --git-commit)")
        return 2
    if protected and any(flag in task_args for flag in (
            "--git-commit", "--round-commit")):
        append_session(root, sess, "ABORTED",
                       reason="protected dirty-slice mode forbids commits")
        log.error("ISOLATE: dirty-slice mode forbids --git-commit/--round-commit")
        return 2
    if patch_file and _inside(root, Path(patch_file)):
        append_session(root, sess, "ABORTED",
                       reason=f"patch target inside repository: {patch_file}")
        log.error("ISOLATE: patch target must be outside repository: %s", patch_file)
        return 2
    return None


def _run_child(workdir: Path, task_args: list,
               protected: bool = False) -> int:
    """在 worktree 里跑 pi-batch 子进程（锁按 cwd 天然隔离）。"""
    args = list(task_args)
    if protected and "--no-git-commit" not in args:
        args.append("--no-git-commit")
    cmd = [sys.executable, "-m", "pbatch", *args]
    log.info("ISOLATE: running: %s (cwd=%s)", " ".join(cmd), workdir)
    return subprocess.run(cmd, cwd=str(workdir),
                          env=_child_env(workdir)).returncode


def _report_task_failure(workdir: Path, exit_code: int) -> None:
    """任务失败诊断提示：worktree 保留现场。"""
    kind = "locked" if exit_code == _LOCK_EXIT else "failed"
    print(f"\n[isolate] TASK {kind.upper()} (exit={exit_code}) — "
          f"worktree left in place for diagnosis:")
    print(f"  cd {workdir} && pi-batch ... --reuse   # 续跑")
    print(f"  git -C {workdir} status                 # 现场")


def run_isolated(root: Path, branch: str, workdir: Path, task_args: list,
                 copy_memory: bool = False,
                 opts: Optional[SessionOptions] = None,
                 seed_dirty: tuple = (), write_scope: tuple = (),
                 patch_file: str = "") -> int:
    """建 worktree → 执行任务 → manual 提示 / 自动合并（账本全程记录）。"""
    requested_root = root.resolve()
    root = _repo_root(requested_root)
    if root is None:
        log.error("ISOLATE: %s is not a git work tree", requested_root)
        return 2
    sess = session_id()
    if opts:
        opts.session_id = sess  # 单一事实源：账本全程同 id
    workdir = workdir.expanduser().resolve()
    options = _slice_options(root, seed_dirty, write_scope, sess)
    if options is None:
        return 2
    seed_paths, scope_paths, source_digest = options
    protected = bool(seed_paths or scope_paths)
    if protected and opts is not None:
        return _reject_protected_merge(root, sess)
    blocked = _preflight(root, branch, workdir, task_args, sess,
                         protected, patch_file)
    if blocked is not None:
        return blocked
    base = _current_branch(root) or "HEAD"
    if not _create_worktree(root, branch, workdir, base, opts, sess, seed_paths):
        return 2
    if copy_memory:
        _copy_memory(root, workdir)
    scope_baseline = _protected_scope_baseline(root, workdir, sess, protected)
    if scope_baseline is None:
        return SCOPE_BLOCKED_EXIT
    exit_code = _run_isolated_task(
        root, workdir, task_args, protected, source_digest,
        scope_paths, scope_baseline, sess, patch_file)
    if exit_code != 0:
        return exit_code
    if opts is None:
        append_session(root, sess, "READY")
        print(f"\n[isolate] ALL PASSED on branch '{branch}'. Merge when ready:")
        print(f"  cd {root} && git merge {branch}")
        print(f"  git worktree remove {workdir}")
        return 0

    # session 模式：任务通过 → 验证后自动合并（Merge Controller 事务）。
    return _finish_session(root, branch, workdir, sess, opts)


def _slice_options(root: Path, seed_dirty: tuple, write_scope: tuple,
                   sess: str) -> Optional[tuple]:
    """Normalize protected-slice options and capture the source baseline."""
    try:
        seed_paths = normalize_patterns(seed_dirty) if seed_dirty else ()
        scope_paths = normalize_patterns(write_scope or seed_paths) if (
            write_scope or seed_paths) else ()
        source_digest = worktree_digest(root) if (seed_paths or scope_paths) else ""
    except (OSError, ValueError) as exc:
        append_session(root, sess, "ABORTED", reason=str(exc))
        log.error("ISOLATE: dirty-slice preflight failed: %s", exc)
        return None
    return seed_paths, scope_paths, source_digest


def _create_worktree(root: Path, branch: str, workdir: Path, base: str,
                     opts: Optional[SessionOptions], sess: str,
                     seed_paths: tuple) -> bool:
    """Create an external worktree and materialize the selected dirty slice."""
    proc = _git(["worktree", "add", str(workdir), "-b", branch, base], root)
    if proc.returncode != 0:
        append_session(root, sess, "ABORTED",
                       reason=f"worktree add failed: {proc.stderr.strip()}")
        log.error("ISOLATE: git worktree add failed: %s", proc.stderr.strip())
        return False
    log.info("ISOLATE: worktree %s (branch %s, from %s)", workdir, branch, base)
    try:
        seeded = seed_dirty_slice(root, workdir, seed_paths) if seed_paths else ()
    except (OSError, ValueError) as exc:
        append_session(root, sess, "ABORTED",
                       reason=f"dirty slice seed failed: {exc}",
                       worktree=str(workdir))
        log.error("ISOLATE: dirty slice seed failed: %s", exc)
        return False
    if seeded:
        log.info("ISOLATE: seeded %d dirty path(s): %s", len(seeded),
                 ", ".join(seeded[:8]))
    append_session(root, sess, "CREATED", branch=branch,
                   worktree=str(workdir), base=opts.base if opts else base,
                   commit=_git(["rev-parse", "--short", "HEAD"], root).stdout.strip())
    return True


def _run_isolated_task(root: Path, workdir: Path, task_args: list,
                       protected: bool, source_digest: str, scope_paths: tuple,
                       scope_baseline: dict, sess: str, patch_file: str) -> int:
    """Run one child and apply protected-slice postconditions."""
    append_session(root, sess, "RUNNING")
    exit_code = _run_child(workdir, task_args, protected)
    log.info("ISOLATE: task exit=%d", exit_code)
    if protected:
        blocked = _check_protected_result(
            root, workdir, source_digest, scope_paths, scope_baseline,
            sess, patch_file)
        if blocked is not None:
            return blocked
    if exit_code != 0:
        append_session(root, sess, "ABORTED", exit_code=exit_code)
        _report_task_failure(workdir, exit_code)
    return exit_code


def _inside(root: Path, candidate: Path) -> bool:
    """True when a resolved candidate is inside the source repository."""
    try:
        candidate.expanduser().resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _reject_protected_merge(root: Path, sess: str) -> int:
    append_session(root, sess, "ABORTED",
                   reason="dirty-slice protection cannot auto-merge")
    log.error("ISOLATE: dirty-slice protection cannot auto-merge")
    return 2


def _protected_scope_baseline(root: Path, workdir: Path, sess: str,
                              protected: bool) -> Optional[dict]:
    """Capture seeded dirty paths before the child is allowed to write."""
    if not protected:
        return {}
    try:
        return scope_snapshot(workdir)
    except (OSError, ValueError) as exc:
        _scope_abort(root, workdir, sess, f"scope baseline failed: {exc}")
        return None


def _check_protected_result(root: Path, workdir: Path, source_digest: str,
                            scope_paths: tuple, scope_baseline: dict,
                            sess: str, patch_file: str) -> Optional[int]:
    """Reject commits, out-of-scope writes, or source-tree drift."""
    try:
        if git_head(workdir) != git_head(root):
            reason = "protected dirty-slice worktree HEAD changed"
            return _scope_abort(root, workdir, sess, reason)
        violations = scope_violations(workdir, scope_paths, scope_baseline)
        if violations:
            reason = "out-of-scope paths: " + ", ".join(violations[:16])
            return _scope_abort(root, workdir, sess, reason)
        if worktree_digest(root) != source_digest:
            reason = "source worktree changed while isolated task ran"
            return _scope_abort(root, workdir, sess, reason)
        if patch_file:
            write_patch(workdir, scope_paths, Path(patch_file))
    except (OSError, ValueError) as exc:
        return _scope_abort(root, workdir, sess, f"scope verification failed: {exc}")
    return None


def _scope_abort(root: Path, workdir: Path, sess: str, reason: str) -> int:
    append_session(root, sess, "ABORTED", reason=reason, worktree=str(workdir))
    log.error("ISOLATE: protected dirty-slice rejected: %s", reason)
    print(f"\n[isolate] SCOPE BLOCKED (exit={SCOPE_BLOCKED_EXIT}): {reason}")
    print(f"  worktree kept for review: {workdir}")
    return SCOPE_BLOCKED_EXIT


def _operational_path_line(porcelain_line: str) -> bool:
    """True for porcelain lines that are runtime data, not worktree
    changes: runtime paths (ledger, memory, session maps, rejected
    excerpts). Matches repo_gates._operational_path semantics."""
    path = porcelain_line[3:].strip()
    return path == ".pi-batch" or path.startswith(".pi-batch/")


def _finish_session(root: Path, branch: str, workdir: Path, sess: str,
                    opts: SessionOptions) -> int:
    """worktree 干净检查 → 根仓库锁（合并队列）→ 合并事务。
    运行时数据目录（用户级 runtime、账本、memory、会话映射）不属于工作区
    变更（真实仓库由 .gitignore 覆盖，陌生/测试仓库必须显式排除），
    否则账本写入会把会话卡在 DIRTY。"""
    porcelain_lines = [line for line in
                       _git(["status", "--porcelain"], workdir).stdout.splitlines()
                       if not _operational_path_line(line)]
    if porcelain_lines:
        append_session(root, sess, "DIRTY",
                       reason="\n".join(porcelain_lines)[:500])
        print("\n[isolate] MERGE BLOCKED: worktree has uncommitted changes "
              "(commit them in the task or pass --git-commit):")
        print(f"  cd {workdir} && git status")
        print(f"  cd {root} && git merge {branch}       # 手动合并")
        return MERGE_BLOCKED_EXIT
    try:
        lock = acquire_lock(str(root), wait_minutes=opts.wait_minutes)
    except SystemExit as exc:
        append_session(root, sess, "QUEUE_REFUSED")
        print("\n[isolate] MERGE QUEUE BUSY: another session/instance holds "
              "the main-repository lock (exit 5)")
        print("  retry with --wait-minutes N to queue, or run when free")
        print(f"  worktree kept: {workdir} (review before merge):")
        print(f"  cd {root} && git merge {branch}")
        return int(exc.code)
    try:
        return merge_session_block(root, branch, workdir, opts)
    finally:
        release_lock(lock)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch.py isolate",
        description="Run pi-batch in an isolated git worktree (one SSH "
                    "session = one worktree; --auto-merge validates then "
                    "merges back to the main branch).")
    parser.add_argument("-b", "--branch", required=True,
                        help="new branch for this task (must not exist)")
    parser.add_argument("-d", "--dir", default="",
                        help="worktree directory (default: <parent>/<repo>-<branch>)")
    parser.add_argument("--copy-memory", action="store_true",
                        help="copy runtime sessions/memory from the main "
                             "worktree to continue context")
    parser.add_argument("--seed-dirty", action="append", default=[], metavar="PATH",
                        help="seed this workspace-relative dirty path/pattern into "
                             "the external worktree; repeat per path")
    parser.add_argument("--write-scope", action="append", default=[], metavar="PATH",
                        help="allow only this workspace-relative path/pattern to "
                             "change in dirty-slice mode; defaults to --seed-dirty")
    parser.add_argument("--patch-file", default="", metavar="FILE",
                        help="write the successful scoped tracked patch outside "
                             "the repository (untracked paths get a sidecar)")
    parser.add_argument("--auto-merge", action="store_true",
                        help="session mode: after the task passes, fetch + "
                             "rebase latest main, run the validation gate, "
                             "auto-merge to the main branch, then clean up")
    parser.add_argument("--gate", default="",
                        help="validation command run in the worktree before "
                             "merge (default: 'make ci' when the repo Makefile "
                             "has a ci target; auto-merge is refused otherwise)")
    parser.add_argument("--base", default="main",
                        help="main branch name to rebase onto and merge into "
                             "(default: main; remote ref origin/<base> is used "
                             "when a remote exists)")
    parser.add_argument("--squash", action="store_true",
                        help="fold the session's commits into one on merge")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="keep the worktree and branch after a successful "
                             "merge (inspection)")
    parser.add_argument("--wait-minutes", type=int, default=0,
                        help="merge queue wait: 0 refuses (exit 5) while "
                             "another session holds the main-repository lock; "
                             ">0 polls until free or the budget expires")
    parser.add_argument("task_args", nargs=argparse.REMAINDER,
                        help="args after '--' passed to pi-batch unchanged")
    return parser


def sessions_main(argv: list) -> int:
    """`pi-batch isolate sessions`：session 状态账本（新→旧）。"""
    parser = argparse.ArgumentParser(prog="pi-batch.py isolate sessions")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    cwd = Path.cwd().resolve()
    root = _repo_root(cwd)
    if root is None:
        log.error("ISOLATE: %s is not a git work tree", cwd)
        return 2
    records = read_sessions(root, args.limit)
    if args.json:
        print(__import__("json").dumps(records, ensure_ascii=False, indent=2))
        return 0
    for record in records:
        print(f"{record.get('status', '?'):<14} {record.get('session_id', '?'):<12} "
              f"{record.get('branch', '?'):<28} {record.get('ts', '')}")
    if not records:
        print("(no sessions recorded; run isolate --auto-merge to start one)")
    return 0


def isolate_main(argv: list) -> None:
    if argv and argv[0] == "sessions":
        sys.exit(sessions_main(argv[1:]))
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
    opts = None
    if args.auto_merge:
        if args.seed_dirty or args.write_scope or args.patch_file:
            parser.error("dirty-slice protection cannot be combined with --auto-merge")
        opts = SessionOptions(
            session_id=session_id(), base=args.base, gate=args.gate,
            squash=args.squash, no_cleanup=args.no_cleanup,
            wait_minutes=args.wait_minutes)
    code = run_isolated(
        root, args.branch, workdir, task_args, args.copy_memory, opts,
        tuple(args.seed_dirty), tuple(args.write_scope), args.patch_file)
    sys.exit(code)