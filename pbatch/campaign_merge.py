"""Fail-closed merge controller for successful isolated campaign directions.

A campaign pipeline may pass its own design/acceptance gates and commit its
isolated branch, but that is not sufficient to change the repository branch.
This module performs a second, repository-bound transaction:

    clean root -> rebase -> diff check -> gate evidence -> tests -> merge

Any failure leaves the source worktree and branch in place for diagnosis.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .campaign_state import digest, failure_fingerprint, git_worktree_evidence
from .pipeline_status import PipelineStatus
from .lock import acquire_lock, release_lock
from .text_io import read_text_bounded

_MAX_OUTPUT = 64 * 1024
_MAX_GATE_OUTPUT = 8 * 1024
_VERDICT_PASS = re.compile(r"^\s*VERDICT\s*:\s*PASS\b", re.IGNORECASE | re.MULTILINE)


@dataclass
class MergeResult:
    status: str
    reason: str = ""
    source_commit: str = ""
    merge_commit: str = ""
    changed_paths: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    gate_output: str = ""
    cleanup_errors: list[str] = field(default_factory=list)


def record_campaign_outcomes(context, outcomes, fingerprints, event) -> None:
    """Optionally merge passed branches, then publish bounded state evidence."""
    for outcome in outcomes:
        _merge_if_enabled(context, outcome)
        _record_outcome(context, outcome,
                        fingerprints[outcome.direction.direction_id], event)


def _merge_if_enabled(context, outcome) -> None:
    if (getattr(context.args, "auto_merge", False)
            and outcome.status == PipelineStatus.PASSED):
        _auto_merge_outcome(context, outcome)


def _record_outcome(context, outcome, fingerprint: str, event) -> None:
    dirty = outcome.dirty_evidence or []
    if outcome.status == PipelineStatus.PASSED and not outcome.clean_commit and not dirty:
        dirty = ["repository state evidence unavailable"]
    extra = {"branch": outcome.branch, "commit": outcome.commit,
             "head_commit": outcome.commit, "baseline_commit": outcome.baseline_commit,
             "clean_commit": outcome.clean_commit, "dirty_evidence": dirty,
             "dirty_digest": outcome.dirty_digest,
             "recovery_status": getattr(outcome, "recovery_status", ""),
             "recovery_reason": getattr(outcome, "recovery_reason", ""),
             "recovery_evidence": getattr(outcome, "recovery_evidence", [])}
    if outcome.status != PipelineStatus.PASSED:
        signature = failure_fingerprint(fingerprint, outcome.status, outcome.reason)
        previous = context.store.latest_event(
            outcome.direction.module, outcome.direction.direction_id)
        prior_signature = (previous.get("prior_failure_fingerprint")
                           or previous.get("failure_fingerprint", ""))
        try:
            prior_count = max(0, int(previous.get(
                "prior_failure_count", previous.get("same_failure_count", 0)) or 0))
        except (TypeError, ValueError, OverflowError):
            prior_count = 0
        count = prior_count + 1 if prior_signature == signature else 1
        extra.update({"failure_fingerprint": signature,
                      "failure_status": outcome.status,
                      "same_failure_count": count})
    if outcome.merge_status:
        extra.update({"merge_status": outcome.merge_status,
                      "merge_reason": outcome.merge_reason,
                      "merge_commit": outcome.merge_commit,
                      "merge_evidence": outcome.merge_evidence or []})
    event(context, outcome.direction.module, outcome.direction.direction_id,
          outcome.direction.title, outcome.status, fingerprint, outcome.evidence,
          outcome.reason, outcome.elapsed, extra)


def _auto_merge_outcome(context, outcome) -> None:
    """Run the repository-bound review/merge transaction for one passed branch."""
    args = context.args
    if not outcome.branch or not outcome.evidence:
        return _block_outcome(outcome, "passed isolated pipeline has no mergeable branch evidence")
    lock_path = None
    if getattr(args, "no_lock", False):
        try:
            lock_path = acquire_lock(cwd=str(context.root), no_lock=False,
                                     wait_minutes=getattr(args, "merge_wait_lock", 0))
        except SystemExit:
            return _block_outcome(outcome, "repository merge lock unavailable")
    try:
        result = review_and_merge(
            context.root, Path(outcome.evidence[0]), outcome.branch,
            outcome.baseline_commit, outcome.direction.direction_id,
            getattr(args, "merge_gate", ""), outcome.evidence,
            gate_timeout=max(1, int(getattr(args, "timeout", 0) or 3600)),
            squash=getattr(args, "merge_squash", False),
            forbid_paths=tuple(getattr(args, "merge_forbid_path", ()) or ()))
    except Exception as exc:  # merge controller must fail closed
        return _block_outcome(outcome, f"merge controller error: {exc}")
    finally:
        if lock_path is not None:
            release_lock(lock_path)
    outcome.merge_status = result.status
    outcome.merge_reason = result.reason
    outcome.merge_commit = result.merge_commit
    outcome.merge_evidence = result.evidence
    outcome.evidence.extend(item for item in result.evidence if item not in outcome.evidence)
    if result.status != PipelineStatus.MERGED:
        _block_outcome(outcome, result.reason)
    elif result.cleanup_errors:
        outcome.merge_reason = "merged; cleanup warnings: " + " | ".join(result.cleanup_errors)


def _block_outcome(outcome, reason: str) -> None:
    outcome.status = PipelineStatus.MERGE_BLOCKED
    outcome.reason = reason
    outcome.merge_status = PipelineStatus.MERGE_BLOCKED
    outcome.merge_reason = reason


def review_and_merge(
    root: Path,
    worktree: Path,
    branch: str,
    baseline: str,
    direction_id: str,
    gate_command: str,
    gate_evidence: list[str],
    *,
    gate_timeout: int = 3600,
    squash: bool = False,
    cleanup: bool = True,
    evidence_dir: Path | None = None,
    forbid_paths: tuple[str, ...] = (),
) -> MergeResult:
    """Review and merge one already-passed campaign worktree."""
    result = _preflight(root, worktree, branch, baseline, gate_command)
    if result:
        return _save_result(result, evidence_dir, direction_id, root, branch)
    target = _git_text(root, ["rev-parse", "HEAD"])
    result = _rebase(worktree, target)
    if result:
        return _save_result(result, evidence_dir, direction_id, root, branch)
    changed = _changed_paths(target, worktree)
    if not changed:
        result = MergeResult(PipelineStatus.MERGED, "no changes to merge",
                             _git_text(worktree, ["rev-parse", "HEAD"]), target)
        if cleanup:
            result.cleanup_errors = _cleanup(root, branch, worktree)
        return _save_result(result, evidence_dir, direction_id, root, branch)
    result = _review_diff_and_evidence(target, worktree, changed, gate_evidence)
    if result:
        return _save_result(result, evidence_dir, direction_id, root, branch)
    result = _scope_guard(target, worktree, changed, forbid_paths)
    if result:
        return _save_result(result, evidence_dir, direction_id, root, branch)
    result = _run_merge_gate(root, worktree, branch, direction_id, changed,
                             gate_command, gate_timeout, squash)
    if result.status == PipelineStatus.MERGED and cleanup:
        result.cleanup_errors = _cleanup(root, branch, worktree)
    return _save_result(result, evidence_dir, direction_id, root, branch)


def _scope_guard(target: str, worktree: Path, changed: list[str],
                 forbid_paths: tuple[str, ...]) -> MergeResult | None:
    """Fail closed before the gate runs when a candidate touches a path the
    campaign declared out of scope (e.g. a backend-only run touching web/).

    A merge gate cannot be trusted to police scope on its own: the gate only
    sees what the user wired into it, and a green gate must never widen the
    run's declared scope.  Returns a MERGE_BLOCKED result or None.
    """
    if not forbid_paths:
        return None
    offenders = [path for path in changed
                 if any(_path_forbidden(path, rule) for rule in forbid_paths)]
    if not offenders:
        return None
    listing = "\n".join(f"  {path}" for path in offenders[:20])
    return MergeResult(
        PipelineStatus.MERGE_BLOCKED,
        f"scope guard: candidate changes {len(offenders)} path(s) outside the "
        f"declared scope {list(forbid_paths)}; refusing to merge:\n{listing}",
        source_commit=_git_text(worktree, ["rev-parse", "HEAD"]),
        changed_paths=changed)


def _path_forbidden(path: str, rule: str) -> bool:
    """A rule is a literal path, a directory prefix, or a trailing-slash glob."""
    if not rule:
        return False
    if rule.endswith("/"):
        return path == rule[:-1] or path.startswith(rule)
    return path == rule or path.startswith(rule + "/")


def _run_merge_gate(root: Path, worktree: Path, branch: str, direction_id: str,
                    changed: list[str], command: str, timeout: int,
                    squash: bool) -> MergeResult:
    source = _git_text(worktree, ["rev-parse", "HEAD"])
    code, output = _run_gate(worktree, command, timeout)
    if code != 0:
        return MergeResult(PipelineStatus.MERGE_BLOCKED,
                           f"merge gate failed with exit {code}", source,
                           changed_paths=changed, gate_output=output)
    if not _clean(worktree) or not _clean(root):
        return MergeResult(PipelineStatus.MERGE_BLOCKED,
                           "gate left root or campaign worktree dirty", source,
                           changed_paths=changed, gate_output=output)
    merged, detail = _merge(root, branch, direction_id, squash)
    if not merged:
        return MergeResult(PipelineStatus.MERGE_BLOCKED,
                           f"git merge failed: {detail[:500]}", source,
                           changed_paths=changed, gate_output=output)
    commit = _git_text(root, ["rev-parse", "HEAD"])
    return MergeResult(PipelineStatus.MERGED, "", source, commit, changed,
                       [], output)


def _preflight(root: Path, worktree: Path, branch: str,
               baseline: str, gate_command: str) -> MergeResult | None:
    if not gate_command.strip():
        return MergeResult(PipelineStatus.MERGE_BLOCKED, "merge gate is required")
    if not _branch_name(worktree) == branch:
        return MergeResult(PipelineStatus.MERGE_BLOCKED,
                           f"worktree branch mismatch: expected {branch}")
    if not _clean(root):
        return MergeResult(PipelineStatus.MERGE_BLOCKED,
                           "repository root is dirty; refusing automatic merge")
    if not _clean(worktree):
        return MergeResult(PipelineStatus.MERGE_BLOCKED,
                           "campaign worktree is dirty; refusing automatic merge")
    if baseline and not _is_ancestor(worktree, baseline, "HEAD"):
        return MergeResult(PipelineStatus.MERGE_BLOCKED,
                           "campaign branch is not descended from its baseline")
    if not _git_text(root, ["branch", "--show-current"]):
        return MergeResult(PipelineStatus.MERGE_BLOCKED,
                           "repository root is detached; refusing automatic merge")
    return None


def _rebase(worktree: Path, target: str) -> MergeResult | None:
    if _is_ancestor(worktree, target, "HEAD"):
        return None
    proc = _git(worktree, ["rebase", target])
    if proc.returncode == 0:
        return None
    _git(worktree, ["rebase", "--abort"])
    return MergeResult(PipelineStatus.MERGE_BLOCKED,
                       f"rebase onto current repository HEAD failed: {_detail(proc)}")


def _review_diff_and_evidence(target: str, worktree: Path, changed: list[str],
                              evidence: list[str]) -> MergeResult | None:
    check = _git(worktree, ["diff", "--check", f"{target}...HEAD"])
    if check.returncode != 0:
        return MergeResult(PipelineStatus.MERGE_BLOCKED,
                           f"git diff --check failed: {_detail(check)}",
                           source_commit=_git_text(worktree, ["rev-parse", "HEAD"]),
                           changed_paths=changed)
    verdicts = [path for path in evidence if _has_pass_verdict(Path(path))]
    if not verdicts:
        return MergeResult(
            PipelineStatus.MERGE_BLOCKED,
            "no bounded VERDICT: PASS artifact supplied by the pipeline",
            source_commit=_git_text(worktree, ["rev-parse", "HEAD"]),
            changed_paths=changed,
        )
    return None


def _has_pass_verdict(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        text = read_text_bounded(path, _MAX_OUTPUT, "merge gate evidence")
    except (OSError, ValueError):
        return False
    return bool(_VERDICT_PASS.search(text))


def _run_gate(worktree: Path, command: str, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(command, shell=True, cwd=str(worktree),
                              capture_output=True, text=True, timeout=timeout,
                              check=False)
    except subprocess.TimeoutExpired as exc:
        output = _bounded(str(exc.stdout or "") + str(exc.stderr or ""))
        return 124, f"gate timed out after {timeout}s\n{output}"
    return proc.returncode, _bounded(proc.stdout + proc.stderr)


def _merge(root: Path, branch: str, direction_id: str, squash: bool) -> tuple[bool, str]:
    # `--no-verify`: the direction's own commit already ran the repository
    # hooks inside its worktree, and the merge gate re-checks scope, the
    # framework-free rule, the forbidden paths, typecheck/build and the test
    # suites. Re-running lint-staged on the squash of several independently
    # merged directions is redundant work that can fail for reasons unrelated
    # to the direction (it re-lints every previously merged file) and would
    # block the campaign with no correctness gain.
    if squash:
        result = _git(root, ["merge", "--squash", branch])
        if result.returncode == 0:
            result = _git(root, ["commit", "--no-verify", "-m",
                                 f"[pi-batch campaign] merge {direction_id}"])
    else:
        result = _git(root, ["merge", "--no-ff", "--no-verify", branch, "-m",
                             f"[pi-batch campaign] merge {direction_id}"])
    if result.returncode != 0:
        _abort_partial_merge(root)
        return False, _detail(result)
    return True, ""


def _abort_partial_merge(root: Path) -> None:
    """Undo a failed automatic merge so the next direction is not blocked.

    A failed `--squash` merge leaves the campaign repository with the merged
    result staged but uncommitted, and a failed non-squash merge leaves
    MERGE_HEAD. Both make the tree dirty, and the merge preflight then refuses
    every later direction ("repository root is dirty"), so one conflicting
    direction would stall the whole campaign until a human intervened. The
    preflight verified a clean repository before merging, so returning to HEAD
    restores exactly the pre-merge state.
    """
    _git(root, ["merge", "--abort"])
    _git(root, ["reset", "--hard", "HEAD"])


def _cleanup(root: Path, branch: str, worktree: Path) -> list[str]:
    errors = []
    removed = _git(root, ["worktree", "remove", "--force", str(worktree)])
    if removed.returncode != 0:
        errors.append(f"worktree remove: {_detail(removed)}")
    deleted = _git(root, ["branch", "-d", branch])
    if deleted.returncode != 0:
        errors.append(f"branch delete: {_detail(deleted)}")
    return errors


def _save_result(result: MergeResult, evidence_dir: Path | None,
                 direction_id: str, root: Path, branch: str) -> MergeResult:
    if evidence_dir is None:
        evidence_dir = config.data_dir(str(root)) / "campaign-merges"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "direction_id": direction_id, "branch": branch, "status": result.status,
        "reason": result.reason, "source_commit": result.source_commit,
        "merge_commit": result.merge_commit, "changed_paths": result.changed_paths,
        "gate_output": result.gate_output[-_MAX_GATE_OUTPUT:],
        "cleanup_errors": result.cleanup_errors,
        "digest": digest([result.status, result.reason, result.changed_paths,
                          result.source_commit, result.merge_commit]),
    }
    target = evidence_dir / f"{direction_id}-merge.json"
    fd, temp = tempfile.mkstemp(prefix=target.name + ".", dir=str(evidence_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temp, target)
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    result.evidence.append(str(target))
    return result


def _changed_paths(target: str, worktree: Path) -> list[str]:
    proc = _git(worktree, ["diff", "--name-only", f"{target}...HEAD"])
    return [line for line in proc.stdout.splitlines() if line.strip()][:1000]


def _clean(path: Path) -> bool:
    proc = _git(path, ["status", "--porcelain=v1", "--untracked-files=all", "--", ".",
                       ":(exclude).pi-batch.lock*", ":(exclude).pi-batch/**"])
    return proc.returncode == 0 and not proc.stdout.strip()


def _branch_name(path: Path) -> str:
    return _git_text(path, ["branch", "--show-current"])


def _is_ancestor(path: Path, ancestor: str, descendant: str) -> bool:
    proc = _git(path, ["merge-base", "--is-ancestor", ancestor, descendant])
    return proc.returncode == 0


def _git_text(path: Path, args: list[str]) -> str:
    proc = _git(path, args)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git(path: Path, args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                              text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git"], 124, "", str(exc))


def _detail(proc: subprocess.CompletedProcess) -> str:
    return _bounded((proc.stderr or "") + (proc.stdout or "")).strip()


def _bounded(value: str) -> str:
    return str(value)[-_MAX_OUTPUT:]
