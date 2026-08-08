"""Repository-level campaign coordinator: discover, analyze, select, implement."""

from __future__ import annotations
from pbatch.pipeline_status import PipelineStatus

import argparse
import logging
import logging.handlers
import math
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .campaign_analysis import (analysis_fingerprint, build_analysis_task,
                                discover_modules, parse_directions,
                                run_analysis_tasks, select_directions)
from .campaign_models import CampaignSettings, Direction
from .campaign_paths import (campaign_config_path as _campaign_config_path,
                             validate_paths as _validate_paths)
from .campaign_pipeline import (direction_fingerprint, parallel_ready,
                                run_direction, run_directions_isolated)
from .campaign_state import StateStore, _git_output, git_snapshot, tool_digest, tool_head, write_summary
from .config import log, yaml
from .lock import acquire_lock, release_lock
from .registry import mark_running, unregister
from .runner import kill_active_procs, run_argv
from .text_io import read_text_bounded


@dataclass
class CampaignContext:
    root: Path
    settings: CampaignSettings
    args: argparse.Namespace
    store: StateStore
    snapshot: dict


def build_campaign_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch.py campaign",
        description="Discover repository modules and run evidence-backed SDLC campaigns")
    parser.add_argument("--config", default="examples/repository-campaign.yaml")
    parser.add_argument("--modules", default="", help="comma-separated module directories")
    parser.add_argument("--max-directions", type=int)
    parser.add_argument("--top-only", action="store_true")
    parser.add_argument("--skip-passed", action="store_true")
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--jobs", type=int, help="parallel module analyses")
    parser.add_argument("--parallel-pipelines", type=int, help="isolated implementation worktrees")
    parser.add_argument("--pipeline", default="", help="implementation pipeline template override")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--state-file", default="")
    parser.add_argument("--summary-file", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--agent-bin", default=config.AGENT_BIN)
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--retries", type=int, help="analysis retries")
    parser.add_argument("--retry-delay", type=float, default=10)
    parser.add_argument("--log-file", default="logs/full-auto.log")
    parser.add_argument("--stream-output", choices=["auto", "full", "none"],
                        default=config.STREAM_OUTPUT)
    parser.add_argument("--rounds", type=int, default=1,
                        help="repeat the campaign up to N times (0 = infinite; "
                             "fingerprint reuse makes later rounds incremental)")
    parser.add_argument("--round-delay", type=float, default=0, metavar="SECONDS",
                        help="pause between campaign rounds (pacing/rate limit)")
    parser.add_argument("--round-commit", action="store_true",
                        help="git add -A + commit all changes after each round "
                             "(respects .gitignore; advisory, never fails the round)")
    parser.add_argument("--no-lock", action="store_true")
    parser.add_argument("--wait-lock", type=int, default=0, metavar="MINUTES",
                        help="queue behind a held lock (poll 30s up to MINUTES) instead of exit 5")
    parser.add_argument("--memory-mode", choices=["auto", "on", "off"], default=config.MEMORY_MODE)
    _add_metering_args(parser)
    return parser


def _add_metering_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--events-file", default="")
    parser.add_argument("--webhook", default="")
    parser.add_argument("--budget-max", "--max-invocations", dest="budget_max",
                        metavar="N", type=int, default=0,
                        help="cap agent invocations per campaign run")
    parser.add_argument("--daily-budget", "--daily-invocations", dest="daily_budget",
                        metavar="N", type=int, default=0,
                        help="cap agent invocations per UTC day")
    parser.add_argument("--daily-state", default="")


def load_settings(root: Path, args: argparse.Namespace) -> CampaignSettings:
    data = {}
    config_path = _campaign_config_path(root, args.config) if args.config else None
    if config_path and config_path.exists():
        if not yaml:
            raise ValueError("campaign config requires PyYAML")
        try:
            loaded = yaml.safe_load(read_text_bounded(
                config_path, config.INPUT_MAX_BYTES, "campaign config")) or {}
        except Exception as exc:
            raise ValueError(f"invalid campaign YAML: {config_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"campaign config must be a mapping: {config_path}")
        data = loaded
    elif args.config:
        raise ValueError(f"campaign config not found: {config_path}")
    settings = CampaignSettings.from_mapping(data)
    _apply_overrides(settings, args)
    settings.validate()
    _validate_runtime_args(args)
    _validate_paths(root, settings)
    return settings


def _validate_runtime_args(args: argparse.Namespace) -> None:
    values = {"timeout": args.timeout, "retry-delay": args.retry_delay,
              "budget-max": args.budget_max, "daily-budget": args.daily_budget,
              "round-delay": args.round_delay}
    invalid = [name for name, value in values.items()
               if not math.isfinite(float(value)) or value < 0]
    if args.rounds < 0:
        invalid.append("rounds")
    if not str(args.agent_bin).strip():
        invalid.append("agent-bin")
    if invalid:
        raise ValueError("invalid campaign runtime values: " + ", ".join(invalid))


def _apply_overrides(settings: CampaignSettings, args: argparse.Namespace) -> None:
    if args.max_directions is not None:
        settings.max_directions = args.max_directions
    if args.top_only:
        settings.max_directions = 1
    if args.jobs is not None:
        settings.analysis_jobs = args.jobs
    if args.parallel_pipelines is not None:
        settings.pipeline_concurrency = args.parallel_pipelines
    if args.retries is not None:
        settings.analysis_retries = args.retries
    for attr, value in (("pipeline_template", args.pipeline), ("output_dir", args.output_dir),
                        ("state_file", args.state_file), ("summary_file", args.summary_file)):
        if value:
            setattr(settings, attr, value)


def run_campaign(context: CampaignContext) -> int:
    modules = discover_modules(context.root, context.settings, context.args.modules)
    if not modules:
        log.error("CAMPAIGN: no modules found")
        return 1
    if context.args.dry_run:
        _print_plan(context, modules)
        return 0
    _event(context, "__tool__", "__tool__", "", "TOOL_VERSION",
           tool_digest(), [tool_head()],
           reason="tool code digest (pbatch + pi-batch.py + pi-batch.yaml); "
                  "invalidates analysis/direction reuse when the tool changes")
    directions, failures = _analyze_modules(context, modules)
    selected, selection_failures = _select(context, modules, directions)
    failures += selection_failures
    outcomes = _implement(context, selected)
    failures += sum(1 for outcome in outcomes if outcome.status != PipelineStatus.PASSED)
    write_summary(context.store, context.settings.path(context.root, context.settings.summary_file),
                  context.settings.name)
    passed = sum(1 for outcome in outcomes if outcome.status == PipelineStatus.PASSED)
    log.info("CAMPAIGN complete: %d passed, %d failed", passed, failures)
    return 1 if failures else 0


def _analyze_modules(context: CampaignContext, modules: list[str]) -> tuple[dict[str, list[Direction]], int]:
    tasks = {}
    fingerprints = {}
    directions = {}
    reuse = _reuse_enabled(context.args)
    for module in modules:
        task = build_analysis_task(context.root, context.settings, module,
                                   context.args.model, context.args.provider, context.args.timeout)
        fp = analysis_fingerprint(context.root, context.settings, module, context.snapshot, task)
        fingerprints[module] = fp
        if reuse and context.store.reusable(module, "__analysis__", fp, ("ANALYZED",)):
            parsed = _parse_analysis_file(module, task)
            if parsed:
                directions[module] = parsed
                continue
        _event(context, module, "__analysis__", "", "ANALYZING", fp, [str(task.output_path())])
        tasks[module] = task
    module_by_output = {str(task.output_path()): module for module, task in tasks.items()}
    def _valid(result):
        module = module_by_output[str(result.task.output_path())]
        return bool(_parse_analysis_file(module, result.task))
    results = run_analysis_tasks(list(tasks.values()), context.settings.analysis_jobs,
                                 context.settings.analysis_retries, context.args.retry_delay, _valid)
    failures = 0
    for module, task in tasks.items():
        result = results.get(str(task.output_path()))
        parsed = _parse_analysis_file(module, task) if result and result.success else []
        if not parsed:
            task.output_path().unlink(missing_ok=True)
            reason = result.reason if result else "analysis produced no structured directions"
            _event(context, module, "__analysis__", "", "ANALYSIS_FAILED",
                   fingerprints[module], [], reason)
            failures += 1
            continue
        directions[module] = parsed
        _event(context, module, "__analysis__", "", "ANALYZED", fingerprints[module],
               [str(task.output_path())], elapsed=result.elapsed)
    return directions, failures


def _parse_analysis_file(module: str, task) -> list[Direction]:
    path = task.output_path()
    try:
        return parse_directions(module, read_text_bounded(
            path, config.OUTPUT_MAX_BYTES, "campaign analysis"))
    except (OSError, ValueError):
        return []


def _select(context: CampaignContext, modules: list[str],
            directions: dict[str, list[Direction]]) -> tuple[list[tuple[Direction, Path, str]], int]:
    selected_items = []
    failures = 0
    for module in modules:
        candidates = directions.get(module, [])
        selected, rejected = select_directions(
            context.root, candidates, context.settings.max_directions, context.settings.minimum_score)
        analysis = _analysis_path(context, module)
        for direction, reason in rejected:
            _direction_event(context, direction, "DIRECTION_REJECTED", "", analysis, reason)
        if candidates and not selected:
            failures += 1
        for direction in selected:
            fp = direction_fingerprint(context.root, context.settings, direction, analysis,
                                       context.snapshot, context.args.model, context.args.provider)
            if (_reuse_enabled(context.args) and context.store.reusable(
                    module, direction.direction_id, fp, (PipelineStatus.PASSED,))):
                continue
            _direction_event(context, direction, "SELECTED", fp, analysis)
            selected_items.append((direction, analysis, fp))
    return selected_items, failures


def _analysis_path(context: CampaignContext, module: str) -> Path:
    task = build_analysis_task(context.root, context.settings, module)
    return task.output_path()


def _implement(context: CampaignContext, items: list[tuple[Direction, Path, str]]):
    if not items:
        return []
    for direction, analysis, fp in items:
        _direction_event(context, direction, "RUNNING", fp, analysis)
    if context.settings.pipeline_concurrency > 1:
        raw = [(direction, analysis) for direction, analysis, _ in items]
        outcomes = run_directions_isolated(context.root, context.settings, raw,
                                           context.settings.pipeline_concurrency,
                                           _agent_args(context.args))
    else:
        outcomes = [_run_direction_safe(context, direction, analysis)
                    for direction, analysis, _ in items]
    fingerprints = {direction.direction_id: fp for direction, _, fp in items}
    for outcome in outcomes:
        _event(context, outcome.direction.module, outcome.direction.direction_id,
               outcome.direction.title, outcome.status, fingerprints[outcome.direction.direction_id],
               outcome.evidence, outcome.reason, outcome.elapsed,
               {"branch": outcome.branch, "commit": outcome.commit})
    return outcomes


def _run_direction_safe(context: CampaignContext, direction: Direction,
                        analysis: Path):
    from .campaign_pipeline import PipelineOutcome
    try:
        return run_direction(context.root, context.settings, direction, analysis,
                             context.args.model, context.args.provider, context.args.timeout,
                             _reuse_enabled(context.args))
    except Exception as exc:
        return PipelineOutcome(direction, "PIPELINE_FAILED", str(exc), 0, [str(analysis)])


def _agent_args(args: argparse.Namespace) -> list[str]:
    values = ["--agent-bin", args.agent_bin, "--memory-mode", args.memory_mode,
              "--stream-output", args.stream_output]
    for flag, value in (("--model", args.model), ("--provider", args.provider),
                        ("--timeout", args.timeout)):
        if value:
            values.extend([flag, str(value)])
    return values


def _event(context: CampaignContext, module: str, direction_id: str, direction: str,
           status: str, fingerprint: str, evidence: list[str], reason: str = "",
           elapsed: float = 0, extra: dict | None = None) -> None:
    payload = {"campaign": context.settings.name, "module": module,
               "direction_id": direction_id, "direction": direction, "status": status,
               "fingerprint": fingerprint, "evidence": evidence, "reason": reason,
               "elapsed": elapsed}
    payload.update(extra or {})
    context.store.append(payload)
    from .memory import record_campaign
    record_campaign(payload, str(context.root))


def _direction_event(context: CampaignContext, direction: Direction, status: str,
                     fingerprint: str, analysis: Path, reason: str = "") -> None:
    _event(context, direction.module, direction.direction_id, direction.title, status,
           fingerprint, [str(analysis)], reason)


def _reuse_enabled(args: argparse.Namespace) -> bool:
    return not args.force and (args.reuse or args.skip_passed or args.retry_failed)


def _print_plan(context: CampaignContext, modules: list[str]) -> None:
    analysis_sec = context.store.median_elapsed(("ANALYZED",), context.settings.analysis_minutes * 60)
    pipeline_sec = context.store.median_elapsed(
        (PipelineStatus.PASSED, PipelineStatus.GATE_REJECTED,
                 PipelineStatus.PIPELINE_FAILED, PipelineStatus.VALIDATION_FAILED),
        context.settings.pipeline_minutes * 60)
    analysis_wall = len(modules) * analysis_sec / context.settings.analysis_jobs
    pipeline_wall = (len(modules) * context.settings.max_directions * pipeline_sec /
                     context.settings.pipeline_concurrency)
    print(f"== Campaign: {context.settings.name} ==")
    print(f"Modules: {len(modules)}; directions/module: {context.settings.max_directions}")
    for module in modules:
        print(f"  - {module}")
    print(f"Estimated analysis: {analysis_wall / 60:.0f} min")
    print(f"Estimated pipelines: {pipeline_wall / 60:.0f} min")
    print(f"Estimated total: {(analysis_wall + pipeline_wall) / 60:.0f} min")
    print("Dry-run made no files or state changes.")


def _wire_runtime(args: argparse.Namespace) -> None:
    from . import metering
    config.AGENT_BIN = args.agent_bin
    config.MEMORY_MODE = args.memory_mode
    config.STREAM_OUTPUT = args.stream_output
    metering.EVENTS_FILE = args.events_file
    metering.WEBHOOK_URL = args.webhook
    metering.BUDGET_MAX = args.budget_max
    metering.DAILY_BUDGET = args.daily_budget
    metering._DAILY_STATE = args.daily_state or str(Path(".pi-batch") / "daily-budget.json")


def main(argv: list[str] | None = None) -> None:
    args = build_campaign_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, _campaign_sigterm)
    root = Path.cwd().resolve()
    try:
        settings = load_settings(root, args)
        if not args.dry_run:
            preflight_errors = config.agent_preflight_errors(args.agent_bin)
            if preflight_errors:
                raise ValueError("agent preflight failed: " + "; ".join(preflight_errors))
        if settings.pipeline_concurrency > 1 and not args.dry_run:
            ready, reason = parallel_ready(root)
            if not ready:
                raise ValueError(reason)
    except ValueError as exc:
        log.error("CAMPAIGN config: %s", exc)
        raise SystemExit(2)
    _wire_runtime(args)
    if args.log_file and not args.dry_run:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(args.log_file, maxBytes=config.LOG_MAX_BYTES,
                                                       backupCount=config.LOG_BACKUP_COUNT, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(handler)
    store = StateStore(settings.path(root, settings.state_file), settings.state_line_max_bytes)
    excluded = _snapshot_excludes(root, settings, args)
    context = CampaignContext(root, settings, args, store, git_snapshot(root, excluded))
    _register_campaign_run(root, args)
    try:
        lock_path = acquire_lock(cwd=str(root), no_lock=args.no_lock or args.dry_run,
                                 wait_minutes=getattr(args, "wait_lock", 0))
        mark_running()
    except SystemExit:
        unregister()
        raise
    try:
        code = run_campaign_loop(context)
    except KeyboardInterrupt:
        log.warning("CAMPAIGN interrupted; rerun with --reuse --retry-failed")
        code = 130
    finally:
        release_lock(lock_path)
        unregister()
    raise SystemExit(code)


def run_campaign_loop(context: CampaignContext) -> int:
    """Run the campaign `--rounds` times (0 = infinite). Later rounds are
    incremental: fingerprint reuse skips PASSED analyses/directions, so a
    round only re-does what the previous round changed (real-world lesson:
    'run it 10 times' previously required an external bash loop)."""
    if context.args.dry_run:
        return run_campaign(context)  # planning is single-shot
    rounds = context.args.rounds
    code = 0
    round_no = 1
    while True:
        if rounds > 0 and round_no > rounds:
            break
        label = str(rounds) if rounds else "\u221e"
        log.info("")
        log.info("========== CAMPAIGN ROUND %d/%s ==========", round_no, label)
        rc = run_campaign(context)
        if rc and code == 0:
            code = rc
        if rounds > 0 and round_no >= rounds:
            break
        if context.args.round_commit:
            _round_commit(context, round_no)
        if context.args.round_delay > 0:
            log.info("ROUND DELAY: sleeping %.0fs before round %d",
                     context.args.round_delay, round_no + 1)
            time.sleep(context.args.round_delay)
        round_no += 1
    return code


def _round_commit(context: CampaignContext, round_no: int) -> None:
    """Commit all non-ignored changes after a round (advisory: a commit
    problem must not fail the round; respects .gitignore via git add -A)."""
    root = context.root
    if not run_argv(["git", "rev-parse", "--git-dir"], str(root), timeout=10).ok:
        log.info("ROUND %d: not a git repository, skipping round commit", round_no)
        return
    status = _git_output(root, ["status", "--porcelain=v1"])
    if not status.strip():
        log.info("ROUND %d: no uncommitted changes", round_no)
        return
    msg = ("[pi-batch] campaign round %d: implemented directions "
           "(see %s)" % (round_no, context.settings.summary_file))
    add = run_argv(["git", "add", "-A"], str(root), timeout=30)
    if not add.ok:
        log.warning("ROUND %d: git add -A failed (exit=%d): %s", round_no,
                    add.exit_code, (add.stderr or "").strip()[:300])
        return
    commit = run_argv(["git", "commit", "-m", msg], str(root), timeout=30)
    if not commit.ok:
        log.warning("ROUND %d: git commit failed (exit=%d): %s", round_no,
                    commit.exit_code, (commit.stderr or "").strip()[:300])
        return
    log.info("ROUND %d: committed %d changed file(s): %s",
             round_no, len(status.splitlines()), msg)


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
    raise KeyboardInterrupt()


def _snapshot_excludes(root: Path, settings: CampaignSettings,
                       args: argparse.Namespace) -> tuple[str, ...]:
    values = [settings.output_dir, settings.worktree_root]
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
