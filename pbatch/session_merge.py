"""Session 合并事务（Merge Controller）——`pi-batch isolate --auto-merge`。

一个 session = 一个 worktree + 一个临时分支。任务在 worktree 内完成后，
由本模块在根仓库单实例锁内串行执行：

    fetch 最新主分支 → rebase session 分支 → 验证门禁 → merge → 清理

全部通过才合并主分支；任一环节失败保留现场（worktree/分支不动）。
合并队列 = 根仓库锁（`--wait-minutes N` 排队轮询，0 = 忙时拒绝 exit 5）。

会话状态账本（追加式，data_dir/sessions.jsonl）状态机：
    CREATED → RUNNING → REBASING → VALIDATING → MERGING → MERGED
异常终态：ABORTED / CONFLICT / TEST_FAILED / NO_GATE / QUEUE_REFUSED /
DIRTY；manual 模式（无 --auto-merge）成功停在 READY（等待人工合并）。
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import data_dir, log

MERGE_BLOCKED_EXIT = 6
_GATE_TIMEOUT_SEC = 3600
_SESSION_LEDGER = "sessions.jsonl"

SESSION_STATES = (
    "CREATED", "RUNNING", "READY", "ABORTED", "DIRTY", "REBASING",
    "CONFLICT", "VALIDATING", "TEST_FAILED", "NO_GATE", "MERGING",
    "MERGED", "QUEUE_REFUSED",
)


@dataclass
class SessionOptions:
    """--auto-merge session 模式的合并契约。"""

    session_id: str = ""
    base: str = "main"
    gate: str = ""
    squash: bool = False
    no_cleanup: bool = False
    wait_minutes: int = 0


def _git(args: list, wd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(wd), *args],
                          capture_output=True, text=True)


# --- session 状态账本（追加式，data_dir/sessions.jsonl） ---------------

def session_id() -> str:
    return "sess-" + uuid.uuid4().hex[:8]


def ledger_path(root: Path) -> Path:
    return data_dir(str(root)) / _SESSION_LEDGER


def append_session(root: Path, session_id: str, status: str, **extra) -> None:
    """追加一条 session 状态记录（有界字段；账本不被清理器改写）。"""
    if status not in SESSION_STATES:
        log.warning("ISOLATE: unknown session state %r", status)
    entry = {"session_id": session_id, "status": status,
             "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    entry.update(extra)
    try:
        path = ledger_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("ISOLATE: session ledger write failed: %s", exc)


def read_sessions(root: Path, limit: int) -> list:
    """读最近 limit 条记录（新→旧，有界行缓冲）。"""
    path = ledger_path(root)
    records = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines[-max(0, limit):][::-1]:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


# --- 合并事务内部步骤 ---------------------------------------------------

def _default_gate(root: Path) -> str:
    """探测仓库标准门禁：Makefile 有 ci target → make ci；否则空。"""
    try:
        text = (root / "Makefile").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "make ci" if any(line.startswith("ci:") for line in text.splitlines()) else ""


def _remote_names(root: Path) -> list:
    proc = _git(["remote"], root)
    return proc.stdout.split() if proc.returncode == 0 else []


def _resolve_base_ref(root: Path, base: str) -> tuple:
    """确定 rebase/merge 基准 ref。有 remote 时先 fetch 最新主分支；
    本地无 base 分支时回退 main→master；仍缺则拒绝并给出 --base 提示。"""
    remotes = _remote_names(root)
    if remotes:
        remote = "origin" if "origin" in remotes else remotes[0]
        fetch = _git(["fetch", remote], root)
        if fetch.returncode != 0:
            return "", f"fetch {remote} failed: {fetch.stderr.strip()}"
        ref = f"{remote}/{base}"
        if _git(["rev-parse", "--verify", "--quiet", ref],
                root).returncode == 0:
            return ref, ""
        if base == "main" and _git(
                ["rev-parse", "--verify", "--quiet",
                 f"{remote}/master"], root).returncode == 0:
            return f"{remote}/master", ""
        return "", (f"base branch '{base}' not found on {remote} "
                    f"(use --base <branch>)")
    if _branch_exists(root, base):
        return base, ""
    if base == "main" and _branch_exists(root, "master"):
        return "master", ""
    return "", (f"base branch '{base}' not found "
                f"(use --base <branch>)")


def _branch_exists(root: Path, branch: str) -> bool:
    proc = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], root)
    return proc.returncode == 0


def _rebase_onto(workdir: Path, ref: str) -> subprocess.CompletedProcess:
    return _git(["rebase", ref], workdir)


def _run_gate(workdir: Path, cmd: str) -> tuple:
    """在 worktree 里跑验证门禁（有界超时与输出摘录）。"""
    try:
        result = subprocess.run(cmd, shell=True, cwd=str(workdir),
                                capture_output=True, text=True,
                                timeout=_GATE_TIMEOUT_SEC)
        output = (result.stdout + result.stderr).strip()[-4000:]
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return 124, f"gate timed out after {_GATE_TIMEOUT_SEC}s"


def _commits_ahead(root: Path, base_ref: str, branch: str) -> int:
    proc = _git(["rev-list", "--count", f"{base_ref}..{branch}"], root)
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip() or 0)
    except ValueError:
        return 0


def _merge_session(root: Path, branch: str, squash: bool) -> tuple:
    """把 session 分支并入主分支：--no-ff（保留提交）或 --squash（单提交）。"""
    if squash:
        proc = _git(["merge", "--squash", branch], root)
        if proc.returncode != 0:
            return False, proc.stderr.strip()
        commit = _git(["commit", "-qm", f"session: {branch} (squashed)"], root)
        return (commit.returncode == 0, commit.stderr.strip())
    proc = _git(["merge", "--no-ff", branch, "-m", f"merge: session {branch}"], root)
    return (proc.returncode == 0, proc.stderr.strip())


def _cleanup_session(root: Path, branch: str, workdir: Path) -> None:
    """合并成功后清理：先确认无未提交的已跟踪变更（防止数据丢失），
    再移除 worktree（--force 清掉门禁产物等未跟踪文件）与 session 分支。"""
    porcelain = _git(["status", "--porcelain"], workdir).stdout
    tracked = [line for line in porcelain.splitlines()
               if not line.startswith("??")]
    if tracked:
        log.warning("ISOLATE: worktree %s has uncommitted tracked changes; "
                    "kept for diagnosis", workdir)
        print(f"  worktree kept (uncommitted tracked changes): {workdir}")
        return
    removed = _git(["worktree", "remove", "--force", str(workdir)], root)
    if removed.returncode != 0:
        log.warning("ISOLATE: worktree remove failed: %s", removed.stderr.strip())
    else:
        print(f"  worktree removed: {workdir}")
    deleted = _git(["branch", "-d", branch], root)
    if deleted.returncode != 0:
        log.warning("ISOLATE: branch delete failed: %s", deleted.stderr.strip())
    else:
        print(f"  branch deleted: {branch}")


# --- 合并事务入口（调用方须持有根仓库锁） --------------------------------

def _finish_merge(root: Path, branch: str, workdir: Path, opts: SessionOptions,
                  base_ref: str, gate_cmd: str) -> int:
    """合并 + 清理；无变更时直接收尾。"""
    ahead = _commits_ahead(root, base_ref, branch)
    if ahead <= 0:
        append_session(root, opts.session_id, "MERGED", note="no changes")
        print("\n[isolate] SESSION no changes to merge; nothing to do.")
        if not opts.no_cleanup:
            _cleanup_session(root, branch, workdir)
        return 0
    append_session(root, opts.session_id, "MERGING")
    merged, err = _merge_session(root, branch, opts.squash)
    if not merged:
        append_session(root, opts.session_id, "CONFLICT", reason=err[:2000])
        print(f"\n[isolate] MERGE BLOCKED: merge conflict on {opts.base}")
        print(f"  cd {root} && git status                # 冲突现场")
        print(f"  git -C {root} merge --abort            # 或解决后提交")
        print(f"  worktree kept: {workdir}")
        return MERGE_BLOCKED_EXIT
    append_session(root, opts.session_id, "MERGED",
                   squash=opts.squash, commits=ahead)
    print(f"\n[isolate] SESSION MERGED into {opts.base}: {branch} "
          f"({ahead} commit(s), {'squashed' if opts.squash else 'kept checkpoints'})")
    print(f"  gate: {gate_cmd} (exit 0)")
    if not opts.no_cleanup:
        _cleanup_session(root, branch, workdir)
    else:
        print(f"  --no-cleanup: worktree and branch kept: "
              f"{workdir} / {branch}")
    return 0


def merge_session_block(root: Path, branch: str, workdir: Path,
                        opts: SessionOptions) -> int:
    """锁内合并事务：fetch → rebase 最新主分支 → 门禁 → merge → 清理。"""
    base_ref, error = _resolve_base_ref(root, opts.base)
    if error:
        append_session(root, opts.session_id, "ABORTED", reason=error)
        print(f"[isolate] ABORTED: {error}")
        return MERGE_BLOCKED_EXIT
    append_session(root, opts.session_id, "REBASING", base=base_ref)
    rebase = _rebase_onto(workdir, base_ref)
    if rebase.returncode != 0:
        append_session(root, opts.session_id, "CONFLICT",
                       reason=rebase.stderr.strip()[-2000:])
        print(f"\n[isolate] MERGE BLOCKED: rebase conflict on {base_ref}")
        print(f"  cd {workdir} && git status            # 冲突现场")
        print(f"  git -C {workdir} rebase --continue    # 解决后继续")
        print(f"  git -C {workdir} rebase --abort       # 或放弃本 session")
        return MERGE_BLOCKED_EXIT
    gate_cmd = opts.gate or _default_gate(root)
    if not gate_cmd:
        append_session(root, opts.session_id, "NO_GATE")
        print("\n[isolate] MERGE BLOCKED: no validation gate detected")
        print("  pass --gate 'CMD' to enable auto-merge (e.g. --gate 'make ci')")
        print("  or merge manually:")
        print(f"  cd {root} && git merge {branch} && git worktree remove {workdir}")
        return MERGE_BLOCKED_EXIT
    append_session(root, opts.session_id, "VALIDATING", gate=gate_cmd)
    code, output = _run_gate(workdir, gate_cmd)
    if code != 0:
        append_session(root, opts.session_id, "TEST_FAILED",
                       gate=gate_cmd, exit_code=code)
        print(f"\n[isolate] MERGE BLOCKED: gate failed (exit {code}): "
              f"{gate_cmd}")
        print("  worktree kept for diagnosis:")
        print(f"  cd {workdir} && {gate_cmd}")
        print(f"  --- gate output tail ---\n{output}\n  --------------------")
        print(f"  cd {root} && git merge {branch}       # 修复后手动合并")
        return MERGE_BLOCKED_EXIT
    return _finish_merge(root, branch, workdir, opts, base_ref, gate_cmd)