"""Fail-closed recovery for repository campaign execution.

A failed campaign attempt may leave source changes behind even when the
failing stage was requirements, design, or review. This module preserves the
attempt in a Git stash plus an audit record, then restores the exact clean
baseline so a retry cannot consume an unreviewed dirty tree. It never
silently touches a tree that was already dirty when the attempt started.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import subprocess
from pathlib import Path

from .campaign_models import CampaignSettings, safe_slug
from .campaign_state import digest
from .config import log
from .text_io import read_text_bounded

MAX_STATUS_LINES = 40
MAX_REASON_BYTES = 2_000
FEEDBACK_MAX_BYTES = 24 * 1024

RECOVERY_RESTORED = "RESTORED"
RECOVERY_NOOP = "NOOP"
RECOVERY_BLOCKED = "BLOCKED"
RECOVERY_DISABLED = "DISABLED"


@dataclass(frozen=True)
class RecoveryResult:
    """Bounded evidence describing one automatic cleanup decision."""

    status: str
    reason: str = ""
    report: str = ""
    stash_ref: str = ""
    backup_ref: str = ""
    dirty_digest: str = ""

    @property
    def evidence(self) -> list[str]:
        return [self.report] if self.report else []


def recovery_attempt_count(root: Path, recovery_root: Path,
                           direction_id: str) -> int:
    """Count prior recovery decisions for one direction without reading them."""
    directory = recovery_root / safe_slug(direction_id)
    if directory.is_symlink() or not directory.is_dir():
        return 0
    return sum(1 for item in directory.glob("*.json")
               if item.is_file() and not item.is_symlink())


def update_round_recovery(context, stalled_rounds: int) -> tuple[int, str]:
    """Return updated stall count and a fail-closed stop reason."""
    if getattr(context, "recovery_blocked", False):
        return stalled_rounds, "CAMPAIGN self-repair blocked; stopping fail-closed"
    settings = getattr(context, "settings", None)
    if not getattr(settings, "recovery_enabled", False):
        return 0, ""
    stalled = (0 if getattr(context, "round_progress", 0)
               else stalled_rounds + 1)
    limit = getattr(settings, "recovery_stop_after_stalled_rounds", 0)
    if limit and stalled >= limit:
        return stalled, ("CAMPAIGN self-repair made no progress for "
                         f"{stalled} round(s); stopping fail-closed")
    return stalled, ""


def has_failed_attempt(run_dir: Path) -> bool:
    """Return true when this direction has a prior failed attempt."""
    if (run_dir / "wreckage.md").is_file():
        return True
    decisions = run_dir / "DECISIONS.md"
    if not decisions.is_file():
        return False
    try:
        text = read_text_bounded(decisions, FEEDBACK_MAX_BYTES,
                                 "prior campaign decisions")
    except (OSError, ValueError):
        return False
    return "FAIL" in text.upper() or "FAILED" in text.upper()


def _recovery_feedback(run_dir: Path) -> str:
    """Extract bounded operational failure facts for a retry prompt."""
    blocks = []
    for name in ("wreckage.md", "DECISIONS.md"):
        path = run_dir / name
        if not path.is_file():
            continue
        try:
            text = read_text_bounded(path, FEEDBACK_MAX_BYTES,
                                     "recovery feedback")
        except (OSError, ValueError):
            continue
        lines = [line.strip()[:300] for line in text.splitlines()
                 if any(token in line.upper() for token in
                        ("FAIL", "FAILED", "TIMEOUT", "REJECT"))]
        if lines:
            blocks.append(name + ":\n" + "\n".join(lines[-20:]))
    return "\n\n".join(blocks)[:FEEDBACK_MAX_BYTES]


def apply_recovery_profile(stages: list, settings: CampaignSettings,
                           run_dir: Path) -> None:
    """Make a failed retry smaller without weakening the final gate."""
    feedback = _recovery_feedback(run_dir)
    note = (
        "\n\nSELF-REPAIR RETRY: the previous attempt failed. Narrow this attempt "
        "to the smallest source-backed acceptance slice. Do not repeat a "
        "large refactor. Requirements, design, review and gate stages are "
        "read-only; only the implementation stage may edit source files. "
        "Verify the failure evidence below and state any unresolved blocker.\n"
        + feedback
    )
    for stage in stages:
        if stage.get("meta"):
            current = int(stage.get("max_roles_per_iteration", 1))
            stage["max_roles_per_iteration"] = min(
                current, settings.recovery_max_roles_per_iteration)
            stage["meta_role_retries"] = 0
            stage["meta_max_failed_roles"] = max(
                int(stage.get("meta_max_failed_roles", 0)),
                settings.recovery_allow_failed_roles)
        if stage.get("name") in {"requirements", "design", "implement"}:
            if stage.get("from_prompt"):
                stage["from_prompt"] += note
            for task in stage.get("tasks", []):
                if isinstance(task, dict) and task.get("prompt"):
                    task["prompt"] += note


def write_wreckage_report(root: Path, pipeline_path: Path) -> None:
    """Record bounded Git residue left by a failed direction."""
    try:
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1"],
                                capture_output=True, text=True, timeout=30, check=False)
        stat = subprocess.run(["git", "-C", str(root), "diff", "--stat"],
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return
    if status.returncode != 0 or not status.stdout.strip():
        return
    lines = ["# Implement wreckage (uncommitted worktree changes at failure)", "",
             "Status: " + ", ".join(sorted(set(line[:2] for line in
                                               status.stdout.splitlines() if line.strip()))),
             "", "```", status.stdout.rstrip(), "```"]
    if stat.returncode == 0 and stat.stdout.strip():
        lines += ["", "## diff --stat", "", "```", stat.stdout.rstrip(), "```"]
    try:
        (pipeline_path.parent / "wreckage.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def recover_worktree(root: Path, baseline: str, expected_branch: str,
                     recovery_root: Path, direction_id: str, *,
                     start_clean: bool, enabled: bool = True,
                     restore: bool = True, reason: str = "",
                     preserve_paths: tuple[Path, ...] = ()) -> RecoveryResult:
    """Preserve a failed attempt and restore its clean Git baseline."""
    before = _git_state(root, preserve_paths)
    dirty_digest = digest(before["status"])
    if not enabled:
        result = RecoveryResult(RECOVERY_DISABLED, "self-repair is disabled",
                                dirty_digest=dirty_digest)
        return _record(recovery_root, direction_id, result, root, before,
                       baseline, expected_branch, reason)
    precondition = _precondition_error(root, before, baseline, expected_branch,
                                       start_clean)
    if precondition:
        return _blocked(recovery_root, direction_id, root, before, baseline,
                        expected_branch, reason, dirty_digest, precondition)
    if not before["status"] and before["head"] == baseline:
        result = RecoveryResult(RECOVERY_NOOP,
                                "failed attempt left a clean baseline",
                                dirty_digest=dirty_digest)
        return _record(recovery_root, direction_id, result, root, before,
                       baseline, expected_branch, reason)
    if not restore:
        return _blocked(recovery_root, direction_id, root, before, baseline,
                        expected_branch, reason, dirty_digest,
                        "recovery refused: restore_on_failure is disabled")
    report_path = _report_path(recovery_root, direction_id)
    stash_ref = _stash_changes(
        root, before["status"], direction_id, preserve_paths)
    if before["status"] and not stash_ref:
        return _blocked(recovery_root, direction_id, root, before, baseline,
                        expected_branch, reason, dirty_digest,
                        "recovery refused: Git stash did not preserve dirty changes",
                        report_path=report_path)
    backup_ref = (_backup_head(root, before["head"], direction_id)
                  if before["head"] != baseline else "")
    if before["head"] != baseline and not backup_ref:
        return _blocked(recovery_root, direction_id, root, before, baseline,
                        expected_branch, reason, dirty_digest,
                        "recovery refused: failed to retain changed HEAD",
                        report_path=report_path, stash_ref=stash_ref)
    return _reset_to_baseline(
        root, recovery_root, direction_id, before, baseline, expected_branch,
        reason, dirty_digest, report_path, stash_ref, backup_ref, preserve_paths)


def _precondition_error(root: Path, before: dict, baseline: str,
                        expected_branch: str, start_clean: bool) -> str:
    if not start_clean:
        return "recovery refused: worktree was dirty before the attempt"
    if not before["head"] or not baseline or not before["branch"]:
        return "recovery refused: Git HEAD, baseline, or branch is unknown"
    if before["branch"] != expected_branch:
        return (f"recovery refused: branch changed from {expected_branch} "
                f"to {before['branch']}")
    if not _is_ancestor(root, baseline, before["head"]):
        return "recovery refused: current HEAD is not descended from baseline"
    return ""


def _reset_to_baseline(root: Path, recovery_root: Path, direction_id: str,
                       before: dict, baseline: str, expected_branch: str,
                       reason: str, dirty_digest: str, report_path: Path,
                       stash_ref: str, backup_ref: str,
                       preserve_paths: tuple[Path, ...]) -> RecoveryResult:
    reset = _git(root, ["reset", "--hard", baseline])
    after = _git_state(root, preserve_paths)
    if reset.returncode != 0 or after["head"] != baseline or after["status"]:
        return _blocked(recovery_root, direction_id, root, before, baseline,
                        expected_branch, reason, dirty_digest,
                        "recovery failed: baseline reset did not produce a clean tree",
                        report_path=report_path, stash_ref=stash_ref,
                        backup_ref=backup_ref)
    result = RecoveryResult(RECOVERY_RESTORED,
                            "failed attempt preserved and worktree restored",
                            str(report_path), stash_ref, backup_ref, dirty_digest)
    return _record(recovery_root, direction_id, result, root, before, baseline,
                   expected_branch, reason, after=after)


def _blocked(recovery_root: Path, direction_id: str, root: Path,
             before: dict, baseline: str, branch: str, failure: str,
             dirty_digest: str, reason: str, *,
             report_path: Path | None = None, stash_ref: str = "",
             backup_ref: str = "") -> RecoveryResult:
    result = RecoveryResult(RECOVERY_BLOCKED, reason,
                            str(report_path) if report_path else "",
                            stash_ref, backup_ref, dirty_digest)
    return _record(recovery_root, direction_id, result, root, before, baseline,
                   branch, failure)


def _stash_changes(root: Path, status: list[str], direction_id: str,
                   preserve_paths: tuple[Path, ...]) -> str:
    if not status:
        return ""
    before = _git_text(root, ["rev-parse", "--verify", "refs/stash"])
    args = ["stash", "push", "--include-untracked", "--message",
            f"pi-batch recovery {safe_slug(direction_id)}", "--", "."]
    args.extend(_exclude_pathspecs(root, preserve_paths))
    result = _git(root, args)
    if result.returncode != 0:
        log.warning("self-repair stash failed: exit=%d", result.returncode)
        return ""
    after = _git_text(root, ["rev-parse", "--verify", "refs/stash"])
    if not after:
        return ""
    if after == before and _git_state(root, preserve_paths)["status"]:
        return ""
    return after


def _backup_head(root: Path, head: str, direction_id: str) -> str:
    if not head:
        return ""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ref = f"refs/ai-batch/recovery/{safe_slug(direction_id)}-{stamp}"
    result = _git(root, ["update-ref", ref, head])
    return ref if result.returncode == 0 else ""


def _record(recovery_root: Path, direction_id: str, result: RecoveryResult,
            root: Path, before: dict, baseline: str, expected_branch: str,
            failure: str, *, after: dict | None = None) -> RecoveryResult:
    try:
        path = Path(result.report) if result.report else _report_path(
            recovery_root, direction_id)
    except (OSError, ValueError) as exc:
        log.warning("self-repair report path unavailable: %s", exc)
        return RecoveryResult(RECOVERY_BLOCKED,
                              f"recovery report path unavailable: {exc}", "",
                              result.stash_ref, result.backup_ref,
                              result.dirty_digest)
    payload = {
        "version": 1,
        "direction_id": direction_id[:200],
        "status": result.status,
        "reason": result.reason[:MAX_REASON_BYTES],
        "failure": failure[:MAX_REASON_BYTES],
        "baseline": baseline[:128],
        "expected_branch": expected_branch[:200],
        "before": _public_state(before),
        "after": _public_state(after or _git_state(root)),
        "stash_ref": result.stash_ref[:200],
        "backup_ref": result.backup_ref[:200],
        "dirty_digest": result.dirty_digest,
    }
    try:
        _atomic_json(path, payload)
    except OSError as exc:
        log.warning("self-repair report unavailable: %s", exc)
        return RecoveryResult(RECOVERY_BLOCKED,
                              f"recovery report write failed: {exc}", "",
                              result.stash_ref, result.backup_ref,
                              result.dirty_digest)
    return RecoveryResult(result.status, result.reason, str(path),
                          result.stash_ref, result.backup_ref,
                          result.dirty_digest)


def _report_path(root: Path, direction_id: str) -> Path:
    if root.is_symlink():
        raise ValueError(f"recovery root must not be a symlink: {root}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = root / safe_slug(direction_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{stamp}.json"


def _git_state(root: Path, preserve_paths: tuple[Path, ...] = ()) -> dict:
    head = _git_text(root, ["rev-parse", "HEAD"])
    branch = _git_text(root, ["branch", "--show-current"])
    result = _git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    raw = result.stdout.splitlines() if result.returncode == 0 else []
    status = [line for line in raw
              if not _runtime_status(line)
              and not _preserved_status(line, root, preserve_paths)][:MAX_STATUS_LINES]
    if result.returncode != 0:
        status = ["<git status unavailable>"]
    return {"head": head, "branch": branch, "status": status}


def _runtime_status(line: str) -> bool:
    path = line[3:].split(" -> ")[-1] if len(line) > 3 else ""
    return (path == ".pi-batch" or path.startswith(".pi-batch/") or
            (path == "docs/evolution/state.jsonl" and line[:2] == "??"))


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def _exclude_pathspecs(root: Path, paths: tuple[Path, ...]) -> list[str]:
    values = [_relative_path(root, path) for path in paths]
    return [f":(exclude){value}" for value in values if value]


def _preserved_status(line: str, root: Path,
                      paths: tuple[Path, ...]) -> bool:
    value = line[3:].split(" -> ")[-1] if len(line) > 3 else ""
    return any(value == relative or value.startswith(relative + "/")
               for relative in (_relative_path(root, path) for path in paths)
               if relative)


def _public_state(state: dict) -> dict:
    return {"head": state.get("head", "")[:128],
            "branch": state.get("branch", "")[:200],
            "status": list(state.get("status", ()))[:MAX_STATUS_LINES]}


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    return _git(root, ["merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0


def _git_text(root: Path, args: list[str]) -> str:
    result = _git(root, args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=60,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git"], 124, "", str(exc))


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False,
                                    sort_keys=True, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
