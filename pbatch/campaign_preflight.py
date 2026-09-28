"""Campaign baseline validation, liveness, and residue reporting."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from . import config
from .campaign_state import _git_output
from .cmd_expand import expand_cmd
from .config import log, validator_applies
from .runner import run_validation


def _provider_breaker_admission(provider: str = "", agent: str = "") -> tuple[str, list[str]]:
    """Return a pause reason/evidence for this campaign's provider only."""
    try:
        from . import provider_breaker
        if not provider_breaker.enabled():
            return "", []
        identity_fn = getattr(provider_breaker, "breaker_identity", None)
        identity = identity_fn(provider, agent) if callable(identity_fn) else ""
        try:
            state = (provider_breaker.breaker_state(provider=identity)
                     if identity else provider_breaker.breaker_state())
        except TypeError:  # compatibility with the pre-partition API
            state = provider_breaker.breaker_state()
        if not state.get("active"):
            return "", []
        remaining = state.get("cooldown_remaining_s", 0)
        scope = f" for {identity}" if identity else ""
        reason = (f"provider breaker active{scope}; cooldown {remaining}s "
                  f"(step {state.get('step', 0)})")
        evidence = [str(state["shared_state_file"])]
        return reason, evidence
    except Exception as exc:
        # Admission is safety-critical: an unreadable breaker cannot silently
        # degrade into spawning more provider work.
        return f"provider breaker state unavailable: {exc}", []


def _preflight_collect_specs(pipeline) -> list:
    """Return unique repo-scoped, deterministic validator specs."""
    from .config import _resolve_validator_specs

    specs = []
    seen = set()
    for stage in pipeline.stages:
        names = [item.strip() for item in (stage.validate_cmd or "").split(",")
                 if item.strip()]
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            for spec in _resolve_validator_specs(name):
                if spec and spec.scope == "repo" and not spec.judge:
                    specs.append((name, spec))
    return specs


def _preflight_run_one(name: str, spec, root: Path) -> bool:
    """Run one applicable baseline gate; old raw-command callers remain valid."""
    if isinstance(spec, str):
        cmd = spec
        applicable = True
    else:
        cmd = spec.cmd
        applicable = validator_applies(spec, str(root))
    if not applicable:
        log.info("PREFLIGHT: %s skipped (not applicable; requires_any=%s)",
                 name, ", ".join(spec.requires_any))
        return True
    rendered = expand_cmd(cmd, "", str(root))
    result = run_validation(
        rendered, str(root), timeout=config.COMMAND_TIMEOUT,
        cap=config.COMMAND_OUTPUT_MAX_BYTES)
    if result.ok:
        log.info("PREFLIGHT: %s OK", name)
        return True
    tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
    log.warning("PREFLIGHT: %s FAILED on baseline (exit=%s)%s",
                name, result.exit_code,
                " — timed out" if result.timed_out else "")
    for line in tail:
        log.warning("PREFLIGHT:   %s", line[:200])
    return False


def _preflight_isolated_runtime(context) -> None:
    """Prove hypothetical worktrees cannot share runtime or lock identities."""
    if getattr(context.settings, "pipeline_concurrency", 1) <= 1:
        return
    from .user_dirs import project_runtime_dir

    worktree_root = context.settings.effective_worktree_root(context.root)
    if worktree_root.is_symlink():
        log.error("PREFLIGHT: worktree root must not be a symlink: %s", worktree_root)
        raise SystemExit(4)
    first = project_runtime_dir(str(worktree_root / "__preflight-a__"))
    second = project_runtime_dir(str(worktree_root / "__preflight-b__"))
    if first == second or first in second.parents or second in first.parents:
        log.error("PREFLIGHT: isolated worktree runtime paths collide")
        raise SystemExit(4)
    if first == config.data_dir(str(context.root)) or second == config.data_dir(str(context.root)):
        log.error("PREFLIGHT: child runtime aliases the coordinator runtime")
        raise SystemExit(4)
    log.info("PREFLIGHT: isolated worktree runtime/lock identities are disjoint")


def _preflight_validators(context) -> int:
    """Validate pipeline schema, isolation and repo gates before model calls."""
    from .campaign_pipeline import load_pipeline

    template = context.settings.path(
        context.root, context.settings.pipeline_template)
    if not template.is_file() or template.is_symlink():
        log.error("PREFLIGHT: pipeline template %s missing or unsafe", template)
        raise SystemExit(4)
    try:
        pipeline = load_pipeline(str(template))
    except (SystemExit, OSError, ValueError, TypeError) as exc:
        log.error("PREFLIGHT: pipeline schema rejected before model calls: %s", exc)
        raise SystemExit(4) from exc
    _preflight_isolated_runtime(context)
    specs = _preflight_collect_specs(pipeline)
    required = tuple(getattr(context.settings, "preflight_required_validators", ()))
    missing = [name for name in required if name not in {item[0] for item in specs}]
    require_any = bool(getattr(context.settings,
                               "preflight_require_repo_validator", False))
    if missing or (require_any and not specs):
        detail = ("missing required repo-scoped validators: " + ", ".join(missing)
                  if missing else "pipeline has no repo-scoped validator")
        log.error("PREFLIGHT: %s", detail)
        raise SystemExit(4)
    if not specs:
        log.warning("PREFLIGHT: no repo-scoped validators in pipeline template")
        return 0
    log.info("PREFLIGHT: %d repo-scoped validator(s) against the target repo",
             len(specs))
    failures = sum(
        0 if _preflight_run_one(name, spec, context.root) else 1
        for name, spec in specs)
    if failures:
        _report_preflight_failures(context, failures)
    return failures


def _report_preflight_failures(context, failures: int) -> None:
    log.warning(
        "PREFLIGHT: %d repo-scoped validator(s) red at HEAD — implement "
        "stages will fail closed on this baseline regardless of agent "
        "quality; clean the baseline (or add exemptions) before running "
        "pipelines, or accept the cost.", failures)
    if getattr(context.args, "preflight_strict", False):
        log.error("PREFLIGHT: --preflight-strict — aborting before pipelines")
        raise SystemExit(4)


def _report_working_tree_residue(context) -> None:
    """Report bounded Git residue left by direction pipelines."""
    result = _git_output(context.root, ["status", "--short"])
    lines = [line for line in (result or "").splitlines() if line.strip()]
    if not lines:
        log.info("WORKTREE: clean (no residue from pipeline stages)")
        return
    modified = sum(1 for line in lines
                   if line.startswith(" M") or line.startswith("M"))
    untracked = sum(1 for line in lines if line.startswith("??"))
    staged = sum(1 for line in lines
                 if line.startswith("A ") or line.startswith("M ")
                 or line.startswith("D "))
    log.warning(
        "WORKTREE: %d uncommitted file(s) left by pipeline stages "
        "(%d modified, %d untracked, %d staged) — triage before merging; "
        "gated-out pipelines still leave verified code in the tree",
        len(lines), modified, untracked, staged)
    for line in lines[:15]:
        log.warning("WORKTREE:   %s", line)
    if len(lines) > 15:
        log.warning("WORKTREE:   ... and %d more", len(lines) - 15)


_HEARTBEAT_SECONDS = 300
_HEARTBEAT_THREAD = None
_HEARTBEAT_LOCK = threading.Lock()


def _start_heartbeat() -> None:
    """Start one low-noise daemon heartbeat per coordinator process."""
    global _HEARTBEAT_THREAD
    with _HEARTBEAT_LOCK:
        if _HEARTBEAT_THREAD is not None and _HEARTBEAT_THREAD.is_alive():
            return
        def beat() -> None:
            while True:
                time.sleep(_HEARTBEAT_SECONDS)
                log.info("CAMPAIGN HEARTBEAT: alive after %s", time.strftime("%H:%M:%S"))

        _HEARTBEAT_THREAD = threading.Thread(
            target=beat, name="campaign-heartbeat", daemon=True)
        _HEARTBEAT_THREAD.start()
