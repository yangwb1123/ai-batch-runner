"""Campaign CLI lifecycle and round supervision.

The execution layer stays focused on discovery/selection/implementation; this
module owns argument wiring, signal recovery, round commits, and the CLI
compatibility surface.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import time
from pathlib import Path

from . import config
from .campaign_pipeline import parallel_ready
from .campaign_recovery import update_round_recovery
from .campaign_state import StateStore, _git_output, digest, git_snapshot, git_worktree_evidence
from .config import log
from .lock import acquire_lock, release_lock
from .registry import mark_running, unregister
from .runner import kill_active_procs, run_argv


class CampaignSignalInterrupt(KeyboardInterrupt):
    """Coordinator-owned SIGTERM, distinct from an operator Ctrl-C."""


def _implementation():
    """Resolve the execution module lazily, avoiding the facade import cycle."""
    from . import campaign_exec
    return campaign_exec


def _wire_runtime(args: argparse.Namespace) -> None:
    from . import metering, ratelimit
    config.AGENT_BIN = args.agent_bin
    config.AGENT_THINKING_OVERRIDE = getattr(args, "thinking", "")
    config.MEMORY_MODE = args.memory_mode
    config.STREAM_OUTPUT = args.stream_output
    if getattr(args, "prompt_overflow", ""):
        config.PROMPT_OVERFLOW = args.prompt_overflow
    # Campaign telemetry is enabled by default in the runtime data directory;
    # an explicit --events-file still provides an isolated ledger.
    metering.EVENTS_FILE = args.events_file or str(
        config.data_dir() / "campaign-events.jsonl")
    metering.WEBHOOK_URL = args.webhook
    metering.BUDGET_MAX = args.budget_max
    metering.DAILY_BUDGET = args.daily_budget
    metering._DAILY_STATE = args.daily_state or str(
        config.data_dir() / "daily-budget.json")
    shared_rate_file = str(config.data_dir() / "rate-limit.json")
    os.environ.setdefault("PBATCH_RATE_LIMIT_FILE", shared_rate_file)
    shared_breaker_file = str(config.data_dir() / "provider-breaker.json")
    os.environ.setdefault("PBATCH_PROVIDER_BREAKER_FILE", shared_breaker_file)
    per_second = getattr(args, "rate_limit", 0) or config.RATE_LIMIT_PER_SECOND
    burst = getattr(args, "rate_burst", 0) or config.RATE_LIMIT_BURST
    ratelimit.configure(per_second, burst, config.RATE_LIMIT_PROVIDERS,
                        os.environ["PBATCH_RATE_LIMIT_FILE"])

def _agent_preflight(args) -> list[str]:
    """Agent readiness, optionally waiting out a short outage.

    An agent binary being reinstalled disappears for a few minutes.  A
    supervisor that restarts the campaign immediately then burns hundreds of
    no-op rounds.  With ``--agent-preflight-wait-seconds`` the campaign waits
    the outage out itself; the default stays fail-fast.
    """
    wait = max(0, int(getattr(args, "agent_preflight_wait_seconds", 0) or 0))
    interval = max(1, int(getattr(args, "agent_preflight_interval", 30) or 30))
    deadline = time.monotonic() + wait
    attempt = 0
    while True:
        attempt += 1
        errors = config.agent_preflight_errors(args.agent_bin)
        if not errors:
            if attempt > 1:
                log.info("agent preflight recovered after %d attempt(s)", attempt)
            return []
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return errors
        log.warning("agent preflight failed (attempt %d, %.0fs left): %s",
                    attempt, remaining, "; ".join(errors))
        time.sleep(min(interval, remaining))


def _parse_campaign(argv: list[str] | None):
    # 惰性导入 CLI 层（campaign.py 在底部 re-export main——避免模块级循环）
    from .campaign import CampaignContext, build_campaign_parser, load_settings
    args = build_campaign_parser().parse_args(argv)
    root = Path.cwd().resolve()
    config.set_data_dir_override(args.data_dir)
    try:
        settings = load_settings(root, args)
        if args.log_file is None:
            args.log_file = str(config.data_dir(str(root)) / "logs" / "campaign.log")
        if not args.dry_run:
            preflight_errors = _agent_preflight(args)
            if preflight_errors:
                raise ValueError(
                    "agent preflight failed: " + "; ".join(preflight_errors))
        if settings.pipeline_concurrency > 1 and not args.dry_run:
            ready, reason = parallel_ready(root)
            if not ready:
                raise ValueError(reason)
    except ValueError as exc:
        log.error("CAMPAIGN config: %s", exc)
        raise SystemExit(2)
    return root, args, settings


def main(argv: list[str] | None = None) -> None:
    from .campaign import CampaignContext
    root, args, settings = _parse_campaign(argv)
    signal.signal(signal.SIGTERM, _campaign_sigterm)
    _wire_runtime(args)
    if args.log_file and not args.dry_run:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(args.log_file, maxBytes=config.LOG_MAX_BYTES,
                                                       backupCount=config.LOG_BACKUP_COUNT, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(handler)
    store = StateStore(settings.state_path(root), settings.state_line_max_bytes,
                       settings.name)
    excluded = _snapshot_excludes(root, settings, args)
    context = CampaignContext(root, settings, args, store, git_snapshot(root, excluded))
    from . import metering
    context.run_id = metering.RUN_ID
    context.round_id = ""
    context.config_fingerprint = digest({
        "settings": settings.__dict__, "model": args.model,
        "provider": args.provider, "thinking": getattr(args, "thinking", ""),
    })
    if not args.dry_run:
        store.append({
            "campaign": settings.name, "module": "__campaign__",
            "direction_id": "__run__", "status": "CAMPAIGN_RUN_STARTED",
            "run_id": context.run_id, "round_id": "",
            "head_commit": str(context.snapshot.get("head", ""))[:128],
            "model": str(args.model)[:120], "provider": str(args.provider)[:120],
            "config_fingerprint": context.config_fingerprint,
        })
        _register_campaign_run(root, args)
    try:
        lock_path = acquire_lock(cwd=str(root), no_lock=args.no_lock or args.dry_run,
                                 wait_minutes=getattr(args, "wait_lock", 0))
        if not args.dry_run:
            mark_running()
    except SystemExit:
        unregister()
        raise
    try:
        code = _run_campaign_supervised(context)
    finally:
        release_lock(lock_path)
        if not args.dry_run:
            unregister()
    raise SystemExit(code)

def _run_campaign_supervised(context: CampaignContext) -> int:
    """Optionally resume after SIGTERM without losing durable campaign state."""
    auto_resume = bool(getattr(context.args, "auto_resume_on_signal", False))
    delay = max(0.0, float(getattr(context.args, "resume_delay", 30)))
    max_resumes = max(0, int(getattr(context.args, "max_signal_resumes", 3)))
    resumes = 0
    while True:
        try:
            return _implementation().run_campaign_loop(context)
        except CampaignSignalInterrupt:
            if not auto_resume or resumes >= max_resumes:
                log.warning("CAMPAIGN interrupted; resume limit reached (%d/%d); "
                            "rerun with --reuse --retry-failed", resumes, max_resumes)
                return 130
            resumes += 1
            log.warning("CAMPAIGN interrupted; preserving state and resuming "
                        "(%d/%d) in %.0fs", resumes, max_resumes, delay)
            if delay:
                time.sleep(delay)
        except KeyboardInterrupt:
            log.warning("CAMPAIGN interrupted; rerun with --reuse --retry-failed")
            return 130


def run_campaign_loop(context: CampaignContext) -> int:
    """Run the campaign `--rounds` times (0 = infinite). Later rounds are
    incremental: fingerprint reuse skips PASSED analyses/directions, so a
    round only re-does what the previous round changed (real-world lesson:
    'run it 10 times' previously required an external bash loop)."""
    if context.args.dry_run:
        return _implementation().run_campaign(context)  # planning is single-shot
    rounds = context.args.rounds
    code = 0
    round_no = 1
    stalled_rounds = 0
    while True:
        if rounds > 0 and round_no > rounds:
            break
        label = str(rounds) if rounds else "\u221e"
        log.info("")
        log.info("========== CAMPAIGN ROUND %d/%s ==========", round_no, label)
        _refresh_repository_snapshot(context)
        context.round_id = f"{getattr(context, 'run_id', 'campaign')}-r{round_no}"
        context.round_progress = 0
        context.recovery_blocked = False
        rc = _implementation().run_campaign(context)
        if rc and code == 0:
            code = rc
        stalled_rounds, stop_reason = update_round_recovery(
            context, stalled_rounds)
        if stop_reason:
            log.error("%s", stop_reason)
            code = 4
            break
        if context.args.round_commit:
            if not _round_commit(context, round_no) and code == 0:
                code = 1
        if rounds > 0 and round_no >= rounds:
            break
        if context.args.round_delay > 0:
            log.info("ROUND DELAY: sleeping %.0fs before round %d",
                     context.args.round_delay, round_no + 1)
            time.sleep(context.args.round_delay)
        round_no += 1
    return code


def _refresh_repository_snapshot(context: CampaignContext) -> None:
    """Refresh repository evidence immediately before every real round."""
    excludes = _snapshot_excludes(context.root, context.settings, context.args)
    context.snapshot = git_snapshot(context.root, excludes)


def _round_commit(context: CampaignContext, round_no: int) -> bool:
    """Durably commit a round, failing closed on unverifiable Git state."""
    root = context.root
    if not run_argv(["git", "rev-parse", "--git-dir"], str(root), timeout=10).ok:
        log.error("ROUND %d COMMIT FAILED: not a Git repository", round_no)
        return False
    status = _git_output(root, ["status", "--porcelain=v1"])
    if not status.strip():
        evidence = git_worktree_evidence(root)
        if evidence["clean_commit"]:
            log.info("ROUND %d: no uncommitted changes; clean commit=%s",
                     round_no, evidence["clean_commit"])
            return True
        log.error("ROUND %d COMMIT FAILED: could not verify Git status; evidence=%s",
                  round_no, evidence["dirty_evidence"])
        return False
    msg = ("[pi-batch] campaign round %d: implemented directions "
           "(see %s)" % (round_no, context.settings.summary_path(context.root)))
    add = run_argv(["git", "add", "-A"], str(root), timeout=30)
    if not add.ok:
        _log_round_commit_failure(root, round_no, "git add -A", add)
        return False
    commit = run_argv(["git", "commit", "-m", msg], str(root), timeout=30)
    if not commit.ok:
        _log_round_commit_failure(root, round_no, "git commit", commit)
        return False
    evidence = git_worktree_evidence(root)
    if not evidence["clean_commit"]:
        log.error("ROUND %d COMMIT FAILED: commit left dirty state digest=%s entries=%s",
                  round_no, evidence["dirty_digest"], evidence["dirty_evidence"])
        return False
    log.info("ROUND %d: committed %d changed file(s); clean commit=%s: %s",
             round_no, len(status.splitlines()), evidence["clean_commit"], msg)
    return True


def _log_round_commit_failure(root: Path, round_no: int, action: str,
                              result) -> None:
    evidence = git_worktree_evidence(root)
    detail = (result.stderr or result.stdout or "").strip()[:300]
    log.error("ROUND %d COMMIT FAILED: %s exit=%d: %s; dirty digest=%s entries=%s",
              round_no, action, result.exit_code, detail,
              evidence["dirty_digest"], evidence["dirty_evidence"])

def _register_campaign_run(root: Path, args) -> None:
    """Publish the campaign in the global run registry."""
    import atexit
    from .registry import register_run, unregister
    register_run(mode="campaign", repo=str(root),
                 session_name=getattr(args, "session_name", "") or "")
    atexit.register(unregister)

def _campaign_sigterm(signum, frame) -> None:
    from .campaign_pipeline import kill_isolated_pipelines
    log.warning("CAMPAIGN received SIGTERM; stopping active agents and pipelines")
    kill_active_procs()
    kill_isolated_pipelines()
    raise CampaignSignalInterrupt()

def _snapshot_excludes(root: Path, settings: CampaignSettings,
                       args: argparse.Namespace) -> tuple[str, ...]:
    values = [str(settings.output_path(root)),
              str(settings.effective_worktree_root(root))]
    if args.log_file:
        log_path = Path(args.log_file)
        values.append(str(log_path if log_path.parent == Path(".") else log_path.parent))
    result = []
    for value in values:
        path = settings.path(root, value)
        try:
            result.append(path.relative_to(root).as_posix())
        except ValueError:
            continue
    return tuple(dict.fromkeys(result))
