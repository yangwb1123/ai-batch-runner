"""Concurrent isolated lanes with a serial, gated merge back to the main branch.

Each lane owns a worktree, a branch, its own command and its own gate outcome.
Lanes run concurrently; merges run one at a time through the existing session
merge transaction (rebase onto the current base -> gate -> merge), so the second
lane is always rebased onto the first lane's result instead of merging a stale
tree.

Deliberately *not* implemented here: choosing models, spending budgets, deciding
whether to restart. Callers pass a validated plan and use
:mod:`pbatch.lane_loop` for the anchored window, restart policy and stop
conditions. A failed lane is never force-merged, never retried in place, and its
worktree is preserved for diagnosis.
"""

from __future__ import annotations

import contextlib
import fnmatch
import io
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import session_merge
from .config import data_dir

MAX_LANES = 8
MAX_COMMAND_OUTPUT = 32 * 1024
LANE_TIMEOUT_DEFAULT = 3600


class LanePlanError(ValueError):
    """The plan cannot be executed safely; nothing is spawned or merged."""


@dataclass(frozen=True)
class Lane:
    lane_id: str
    branch: str
    worktree: Path
    command: tuple
    timeout: int = LANE_TIMEOUT_DEFAULT
    scope: tuple = ()          # workspace-relative paths this lane may change

    def validate(self, root: Path) -> None:
        self._validate_names()
        self._validate_scope()
        self._validate_worktree(root)

    def _validate_names(self) -> None:
        if not self.lane_id or len(self.lane_id) > 64:
            raise LanePlanError("lane id must be 1..64 characters")
        if not self.branch or " " in self.branch or self.branch.startswith("-"):
            raise LanePlanError(f"lane {self.lane_id}: invalid branch name")
        if not self.command or not all(isinstance(part, str) and part for part in self.command):
            raise LanePlanError(f"lane {self.lane_id}: command must be a non-empty argv list")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, int) or self.timeout < 1:
            raise LanePlanError(f"lane {self.lane_id}: timeout must be a positive integer")

    def _validate_scope(self) -> None:
        if not isinstance(self.scope, (tuple, list)) or not all(
                isinstance(item, str) and item and not item.startswith("/")
                and ".." not in item.split("/") for item in self.scope):
            raise LanePlanError(f"lane {self.lane_id}: scope must be relative workspace paths")

    def _validate_worktree(self, root: Path) -> None:
        worktree = Path(self.worktree).expanduser().resolve()
        if worktree == root or root in worktree.parents:
            raise LanePlanError(f"lane {self.lane_id}: worktree must be outside the repository")
        if worktree.exists() and any(worktree.iterdir()):
            raise LanePlanError(f"lane {self.lane_id}: worktree directory is not empty")


@dataclass
class LaneOutcome:
    lane: Lane
    status: str
    reason: str = ""
    exit_code: Optional[int] = None
    elapsed_seconds: Optional[float] = None
    merge_status: str = ""
    merge_commit: str = ""
    evidence: list = field(default_factory=list)
    merge_tail: str = ""

    def receipt(self) -> dict:
        return {"lane": self.lane.lane_id, "branch": self.lane.branch,
                "status": self.status, "reason": self.reason[:500],
                "exit_code": self.exit_code, "elapsed_seconds": self.elapsed_seconds,
                "merge_status": self.merge_status, "merge_commit": self.merge_commit,
                "merge_tail": self.merge_tail[-800:]}


def validate_plan(root: Path, lanes: list, gate: str) -> None:
    if not isinstance(lanes, list) or not 1 <= len(lanes) <= MAX_LANES:
        raise LanePlanError("lane plan must contain 1..%d lanes" % MAX_LANES)
    if not isinstance(gate, str) or not gate.strip():
        raise LanePlanError("a merge gate command is required")
    ids, branches, worktrees = set(), set(), set()
    for lane in lanes:
        if not isinstance(lane, Lane):
            raise LanePlanError("lane plan entries must be Lane objects")
        lane.validate(root)
        for value, seen, label in ((lane.lane_id, ids, "lane id"), (lane.branch, branches, "branch")):
            if value in seen:
                raise LanePlanError("duplicate %s: %s" % (label, value))
            seen.add(value)
        resolved = str(Path(lane.worktree).expanduser().resolve())
        if resolved in worktrees:
            raise LanePlanError("duplicate worktree: " + resolved)
        worktrees.add(resolved)


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, check=False)


def _clean(path: Path) -> bool:
    result = _git(["status", "--porcelain"], path)
    return result.returncode == 0 and not result.stdout.strip()


def _branch_exists(root: Path, branch: str) -> bool:
    return _git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
                root).returncode == 0


def preflight(root: Path, lanes: list) -> None:
    """Refuse before creating anything when the merge transaction cannot run."""
    if _git(["rev-parse", "--show-toplevel"], root).returncode != 0:
        raise LanePlanError("not a git work tree: " + str(root))
    if not _clean(root):
        raise LanePlanError("repository root is dirty; commit or stash before lanes")
    for lane in lanes:
        if _branch_exists(root, lane.branch):
            raise LanePlanError("branch already exists: " + lane.branch)


def _run_lane(lane: Lane) -> LaneOutcome:
    started = time.monotonic()
    try:
        result = subprocess.run(list(lane.command), cwd=str(lane.worktree),
                                capture_output=True, text=True, timeout=lane.timeout,
                                check=False)
    except subprocess.TimeoutExpired:
        return LaneOutcome(lane, "lane_timeout", "lane command exceeded its timeout",
                           elapsed_seconds=round(time.monotonic() - started, 3))
    except OSError as exc:
        return LaneOutcome(lane, "lane_failed", "lane command could not start: %s" % exc,
                           elapsed_seconds=round(time.monotonic() - started, 3))
    tail = ((result.stdout or "") + (result.stderr or ""))[-MAX_COMMAND_OUTPUT:]
    status = "lane_passed" if result.returncode == 0 else "lane_failed"
    return LaneOutcome(lane, status,
                       "" if status == "lane_passed" else "lane exited %d: %s" % (result.returncode, tail[-400:]),
                       exit_code=result.returncode,
                       elapsed_seconds=round(time.monotonic() - started, 3),
                       evidence=[line for line in tail.strip().splitlines()[-3:]])


def _create_worktrees(root: Path, lanes: list) -> None:
    for lane in lanes:
        lane.worktree.parent.mkdir(parents=True, exist_ok=True)
        result = _git(["worktree", "add", str(lane.worktree), "-b", lane.branch, "HEAD"], root)
        if result.returncode != 0:
            raise LanePlanError("worktree add failed for %s: %s"
                                % (lane.lane_id, result.stderr.strip()[:300]))


def _scope_allows(path: str, scope: tuple) -> bool:
    return any(path == rule.rstrip("/") or path.startswith(rule.rstrip("/") + "/")
               or fnmatch.fnmatchcase(path, rule) for rule in scope)


def changed_paths(root: Path, lane: Lane) -> list:
    """Paths the lane branch changes relative to the repository HEAD/base."""
    result = _git(["diff", "--name-only", "HEAD...%s" % lane.branch], root)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def scope_violations(root: Path, lane: Lane) -> list:
    if not lane.scope:
        return []
    return [path for path in changed_paths(root, lane) if not _scope_allows(path, lane.scope)]


def _remove_worktree(root: Path, lane: Lane) -> None:
    _git(["worktree", "remove", "--force", str(lane.worktree)], root)
    _git(["branch", "-D", lane.branch], root)


def _merge_one(root: Path, item: LaneOutcome, gate: str, base: str) -> bool:
    options = session_merge.SessionOptions(
        session_id="lane-" + item.lane.lane_id, base=base, gate=gate)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = session_merge.merge_session_block(root, item.lane.branch,
                                                item.lane.worktree, options)
    output = captured.getvalue()
    item.evidence.extend(line for line in output.splitlines()[-4:])
    item.merge_tail = output.strip()[-800:]
    item.merge_status = "merged" if code == 0 else "merge_blocked"
    if code == 0:
        item.merge_commit = _git(["rev-parse", "HEAD"], root).stdout.strip()
        return True
    item.status = "merge_blocked"
    item.reason = "session merge exited %d" % code
    return False


def _block(item: LaneOutcome, blocked: list, status: str, reason: str) -> None:
    item.status = status
    item.reason = reason
    blocked.append(item)


def _merge_ready(root: Path, ready: list, blocked: list, gate: str, base: str) -> list:
    merged = []
    for item in ready:
        violations = scope_violations(root, item.lane)
        if violations:
            _block(item, blocked, "lane_scope_violation",
                   "lane changed paths outside its declared scope: "
                   + ", ".join(violations[:10]))
            item.evidence.extend(violations[:10])
            continue
        if not _clean(item.lane.worktree):
            _block(item, blocked, "lane_uncommitted",
                   "lane worktree still has uncommitted changes; commit inside the lane")
            continue
        if _merge_one(root, item, gate, base):
            merged.append(item)
        else:
            blocked.append(item)
    return merged


def run_round(root: Path, lanes: list, *, gate: str, jobs: int = 2,
              base: str = "main") -> dict:
    """Run the lanes, then merge the passing ones serially through the gate."""
    validate_plan(root, lanes, gate)
    preflight(root, lanes)
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise LanePlanError("jobs must be a positive integer")
    _create_worktrees(root, lanes)
    outcomes = []
    with ThreadPoolExecutor(max_workers=min(jobs, len(lanes))) as pool:
        for outcome in pool.map(_run_lane, lanes):
            outcomes.append(outcome)
    blocked = [item for item in outcomes if item.status != "lane_passed"]
    merged = _merge_ready(root, [item for item in outcomes if item.status == "lane_passed"],
                          blocked, gate, base)
    receipt = {
        "schema_version": 1,
        "at": time.time(),
        "base": base,
        "gate": gate[:300],
        "scopes": {item.lane.lane_id: list(item.lane.scope) for item in outcomes},
        "lanes": [item.receipt() for item in outcomes],
        "merged": [item.lane.lane_id for item in merged],
        "blocked": [item.lane.lane_id for item in blocked],
        "head": _git(["rev-parse", "HEAD"], root).stdout.strip(),
    }
    _append_receipt(root, receipt)
    return receipt


def _append_receipt(root: Path, receipt: dict) -> Path:
    path = data_dir(str(root)) / "lanes" / "rounds.jsonl"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
    return path


def cleanup(root: Path, lanes: list) -> None:
    """Remove only worktrees/branches this round created, when still unused."""
    for lane in lanes:
        worktree = Path(lane.worktree)
        if worktree.exists():
            _remove_worktree(root, lane)
