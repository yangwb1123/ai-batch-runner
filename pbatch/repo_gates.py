"""Repo-scoped validator execution (N2).

Full-tree gates (go build ./..., quality.py, uicheck) must not run per
artifact mid-stage — concurrent role agents are still writing files and
would fail the gate spuriously (misreport -> retry -> burned quota).
Deferred here, they run once at stage end and observe the finished tree.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .config import (_resolve_validator_specs, log, validator_applies)
from .cmd_expand import expand_cmd
from .runner import _json_status, judge_verdict, run_argv, run_validation


def _operational_path(path: str) -> bool:
    return (path == ".pi-batch" or path.startswith(".pi-batch/")
            or path == "docs/evolution/state.jsonl")


def _porcelain_paths(raw: str, excludes: tuple[str, ...] = ()) -> frozenset[str]:
    """Parse ``git status --porcelain=v1 -z`` without losing odd names."""
    fields, paths, index = raw.split("\0"), set(), 0
    excluded = tuple(item.rstrip("/") for item in excludes if item)
    while index < len(fields) and fields[index]:
        entry = fields[index]
        if len(entry) >= 4:
            status, path = entry[:2], entry[3:]
            if not _operational_path(path) and not _excluded_path(path, excluded):
                paths.add(path)
            if ("R" in status or "C" in status) and index + 1 < len(fields):
                index += 1
                if (fields[index] and not _operational_path(fields[index])
                        and not _excluded_path(fields[index], excluded)):
                    paths.add(fields[index])
        index += 1
    return frozenset(paths)


def _excluded_path(path: str, excludes: tuple[str, ...]) -> bool:
    return any(path == item or path.startswith(item + "/")
               for item in excludes)


def _relative_excludes(root: str, excludes: list[str]) -> tuple[str, ...]:
    values = []
    base = Path(root).resolve()
    for value in excludes:
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(base)
            except ValueError:
                continue
        values.append(path.as_posix())
    return tuple(values)


def capture_stage_dirty_paths(workdir: str, excludes: list[str] | tuple[str, ...] = ()) -> tuple[str, object]:
    """Snapshot dirty/staged/untracked paths relative to the repository root."""
    wd = workdir or os.getcwd()
    root_result = run_argv(["git", "rev-parse", "--show-toplevel"], wd, timeout=10)
    if not root_result.ok:
        return "", frozenset()
    root = str(Path(root_result.stdout.strip()).resolve())
    status = run_argv(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        root, timeout=10)
    if not status.ok:
        log.warning("git dirty baseline failed; later stage commit will be refused")
        return root, None
    return root, _porcelain_paths(
        status.stdout, _relative_excludes(root, list(excludes)))


def _dirty_digests(root: str, paths) -> dict[str, str]:
    """Content digest per dirty path, so a read-only stage can tell whether it
    MODIFIED an already-dirty file rather than merely coexisting with it.

    Missing/unreadable entries get a sentinel instead of raising: a deleted or
    permission-denied worktree file must not crash an unattended run, and the
    sentinel differs from any real digest, so a transition into or out of that
    state still registers as a change.
    """
    digests: dict[str, str] = {}
    for rel in paths:
        candidate = Path(root) / rel
        try:
            if candidate.is_symlink() or not candidate.is_file():
                digests[rel] = "absent"
                continue
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            digests[rel] = digest.hexdigest()
        except OSError:
            digests[rel] = "unreadable"
    return digests


def capture_stage_baseline(workdir: str,
                           excludes: list[str] | tuple[str, ...] = (),
                           *, read_only: bool = False,
                           ) -> tuple[str, object, object] | tuple[str, object]:
    """Snapshot the dirty baseline for a stage.

    ``read_only`` additionally captures per-path content digests, so a
    read-only stage can be validated against a worktree that was ALREADY dirty
    when the stage started — the normal case for a shared worktree, and
    explicitly expected by the campaign pipeline template that tells
    implementers to preserve unrelated dirty changes.
    """
    root, paths = capture_stage_dirty_paths(workdir, excludes)
    if not read_only or paths is None or not root:
        return root, paths
    return root, paths, _dirty_digests(root, sorted(paths))


def _relative_repo_path(path: str, root: str, wd: str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(wd) / candidate
    try:
        return candidate.resolve().relative_to(Path(root)).as_posix()
    except (OSError, ValueError):
        return ""


def _stage_owned_paths(outputs: list, wd: str,
                       baseline: tuple[str, object]) -> list[str] | None:
    # Indexed rather than unpacked: a read-only baseline carries a third element
    # (content digests) that ownership does not need. See
    # capture_read_only_baseline().
    root, before = baseline[0], baseline[1]
    end_root, after = capture_stage_dirty_paths(wd)
    if before is None or after is None or not root or root != end_root:
        log.warning("git commit refused: stage dirty baseline unavailable or changed")
        return None
    declared = {_relative_repo_path(path, root, wd) for path in outputs}
    declared.discard("")
    owned = ((declared | (set(after) - set(before))) - set(before)) & set(after)
    return [str(Path(root) / path) for path in sorted(owned)
            if not _operational_path(path)]


def _commit_path_sets(outputs: list, wd: str) -> tuple[list, list, list]:
    paths = [path for path in outputs if run_argv(
        ["git", "check-ignore", "-q", "--", path], wd,
        timeout=10).exit_code != 0]
    staged = [path for path in paths if run_argv(
        ["git", "diff", "--cached", "--quiet", "--", path], wd,
        timeout=10).exit_code != 0]
    untracked = [path for path in paths if not run_argv(
        ["git", "ls-files", "--error-unmatch", "--", path], wd,
        timeout=10).ok]
    return paths, staged, untracked


def _restore_owned_index(paths: list, wd: str) -> None:
    if run_argv(["git", "rev-parse", "--verify", "HEAD"], wd, timeout=10).ok:
        run_argv(["git", "reset", "-q", "HEAD", "--", *paths], wd, timeout=10)
    else:
        run_argv(["git", "rm", "--cached", "-q", "--ignore-unmatch",
                  "--", *paths], wd, timeout=10)


def _add_intent_to_add(paths: list, wd: str) -> bool:
    if not paths:
        return True
    added = run_argv(["git", "add", "-N", "--", *paths], wd, timeout=10)
    if added.ok:
        return True
    log.warning("git add failed (exit=%d): %s", added.exit_code,
                (added.stderr or "").strip()[:500])
    return False


def _report_commit_failure(commit, paths: list, untracked: list, wd: str,
                           has_baseline: bool) -> bool:
    if has_baseline:
        _restore_owned_index(paths, wd)
    elif untracked:
        run_argv(["git", "rm", "--cached", "-q", "--ignore-unmatch",
                  "--", *untracked], wd, timeout=10)
    log.warning("git commit failed (exit=%d): %s", commit.exit_code,
                (commit.stderr or "").strip()[:500])
    return False


def read_only_violations(stage, outputs: list, dirty_baseline) -> list[str]:
    """Return source paths changed by a declared read-only stage.

    A read-only stage must not INTRODUCE changes. It does not need an empty
    worktree to prove that: the baseline is compared instead of being required
    to be empty. Requiring emptiness made every read-only stage fail closed on
    a shared dirty worktree (campaign logs 2026-09-18/19: 43 occurrences, each
    halting the pipeline at requirements), which contradicts the campaign
    template's own instruction to preserve unrelated dirty changes.

    Violations are:
      * paths that became dirty during the stage but are not declared outputs;
      * pre-existing dirty paths whose CONTENT changed during the stage (only
        checkable when the baseline carried digests).
    """
    if dirty_baseline is None:
        return ["read-only baseline unavailable"]
    root, before = dirty_baseline[0], dirty_baseline[1]
    before_digests = dirty_baseline[2] if len(dirty_baseline) > 2 else None
    if not root and before == frozenset():
        log.warning("read-only stage cannot observe source changes without Git; "
                    "continuing for non-Git compatibility")
        return []
    if before is None or not root:
        return ["read-only Git baseline unavailable"]
    end_root, after = capture_stage_dirty_paths(
        stage.cwd or os.getcwd(), stage.read_only_paths)
    if end_root != root or after is None:
        return ["read-only Git state unavailable after stage"]
    declared = {_relative_repo_path(path, root, stage.cwd or os.getcwd())
                for path in outputs}
    declared.discard("")
    previous = set(before)
    violations = set(after) - previous - declared
    if before_digests:
        for path, digest in _dirty_digests(root, sorted(previous & set(after))).items():
            if path in declared:
                continue
            if before_digests.get(path) != digest:
                violations.add(path)
    return sorted(violations)


def commit_stage_outputs(stage, outputs: list, dirty_baseline=None) -> bool:
    """Commit only stage outputs, preserving every pre-existing index entry."""
    if not stage.git_commit or stage.read_only:
        return True
    wd = stage.cwd or os.getcwd()
    if not run_argv(["git", "rev-parse", "--git-dir"], wd, timeout=10).ok:
        log.warning("Not a git repository, skipping git commit")
        return False
    candidates = (_stage_owned_paths(outputs, wd, dirty_baseline)
                  if dirty_baseline is not None else outputs)
    if candidates is None:
        return False
    if not candidates:
        log.info("GIT COMMIT: no stage-owned changes — skipping")
        return True
    paths, staged, untracked = _commit_path_sets(candidates, wd)
    if not paths:
        log.info("GIT COMMIT: all %d output(s) gitignored — skipping", len(outputs))
        return True
    if staged and dirty_baseline is None:
        log.warning("git commit refused: output already staged (preserving user index): %s",
                    ", ".join(staged))
        return False
    if not _add_intent_to_add(untracked, wd):
        return False
    message = stage.commit_message or (
        "[pi-batch] Stage: %s - %d tasks completed" % (stage.name, len(outputs)))
    commit = run_argv(
        ["git", "commit", "--only", "-m", message, "--", *paths], wd, timeout=900)
    if not commit.ok:
        return _report_commit_failure(
            commit, paths, untracked, wd, dirty_baseline is not None)
    log.info("GIT COMMIT: %s (files: %d)", message, len(paths))
    return True


def run_stage_repo_validators(stage, results: list, outputs: list,
                              effective_validate=None) -> tuple[bool, str]:
    """N2 (round-5 finding): run repo-scoped validators once at stage end.
    Specs are collected from the stage's validate_cmd plus every task's
    per-task validate field (deduplicated); a failure fails the stage
    (gate semantics). Returns (ok, failing-command-or-empty)."""
    configured = [effective_validate if effective_validate is not None
                  else stage.validate_cmd]
    configured.extend(task.get("validate", task.get("validate_cmd"))
                      for task in stage.tasks if isinstance(task, dict))
    configured.extend(r.task.validate for r in results if r.task.validate)
    specs = _collect_repo_specs(configured)
    specs = _applicable_repo_specs(specs, stage.cwd or os.getcwd(), stage.name)
    if not specs:
        return True, ""
    wd = stage.cwd or os.getcwd()
    output_dir = str(Path(outputs[0]).parent) if outputs else wd
    for spec in specs:
        cmd = expand_cmd(spec.cmd, output_dir, wd)
        ok, detail = _run_stage_repo_spec(spec, cmd, wd, stage.name)
        if not ok:
            return False, detail
    log.info("REPO VALIDATE PASS (stage '%s'): %d gate(s)", stage.name, len(specs))
    return True, ""


def _collect_repo_specs(raw_specs: list) -> list:
    """Collect and deduplicate repo-scoped validator specs from raw strings."""
    specs: list = []
    seen = set()
    for raw in raw_specs:
        for spec in _resolve_validator_specs(raw or ""):
            key = (spec.cmd, spec.requires_any)
            if spec.scope == "repo" and key not in seen:
                seen.add(key)
                specs.append(spec)
    return specs


def _applicable_repo_specs(specs: list, wd: str, label: str) -> list:
    """Keep applicable repo gates and log explicit not-applicable skips."""
    applicable = []
    for spec in specs:
        if validator_applies(spec, wd):
            applicable.append(spec)
            continue
        log.info("REPO VALIDATE SKIP (stage '%s'): %s not applicable; "
                 "requires_any=%s", label, spec.cmd, ", ".join(spec.requires_any))
    return applicable


def _run_stage_repo_spec(spec, cmd: str, wd: str, stage_name: str) -> tuple[bool, str]:
    """Run one repo-scoped validator; (True, "") on success, (False, cmd)
    on failure so the caller can fail the stage with the gate command as
    the detail."""
    log.info("REPO VALIDATE (stage '%s'): %s", stage_name, cmd)
    v = run_validation(cmd, wd)
    if not v.ok:
        log.error("REPO VALIDATE FAILED%s: %s",
                  " (timeout)" if v.timed_out else f" (exit={v.exit_code})", cmd)
        for line in (v.stderr or "").strip().splitlines()[-10:]:
            log.error("  | %s", line)
        return False, cmd
    status = _json_status(v.stdout)
    if status == "fail":
        log.error("REPO VALIDATE FAILED (JSON status=fail): %s", cmd)
        return False, cmd
    if spec.judge:
        verdict = judge_verdict(v.stdout)
        if verdict in ("FAIL", "REJECT"):
            log.error("REPO VALIDATE FAILED (judge verdict=%s): %s", verdict, cmd)
            return False, cmd
        if verdict is None:
            log.error("REPO VALIDATE FAILED (judge: no verdict): %s", cmd)
            return False, cmd
    if status == "warn":
        log.warning("REPO VALIDATE WARN: %s", cmd)
    return True, ""
