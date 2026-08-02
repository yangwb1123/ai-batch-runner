"""Command-line entry points: parser, main, round loop, and the
single-batch / pipeline dispatch."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import config
from .config import (AGENT_BIN, AGENT_DEFAULT_WORKERS, COMMIT_PREFIX_DEFAULT, log, yaml)
from .models import Pipeline, Task
from .reuse import (reuse_decision, reuse_fingerprint, sidecar_path, write_sidecar)
from .lock import LOCK_EXIT_CODE, acquire_lock, release_lock, verify_held
from .triage import (STALL_EXIT_CODE, Circuit, StallDetector, env_signature,
                     residue_markers, task_key)
from .pipeline import (_append_decision_log, _archive_outputs, load_pipeline,
                       load_tasks, load_tasks_from_dir, run_pipeline)
from .runner import print_summary, run_parallel, run_serial

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="pi-batch -- serial/parallel batch executor for pi agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    _add_source_args(p)
    _add_runtime_args(p)
    _add_batch_args(p)
    return p


def _add_source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", nargs="?",
                   help="YAML task file / JSON file / plain text prompt")
    p.add_argument("-p", "--prompt", help="inline prompt (single task shortcut)")
    p.add_argument("-o", "--output", help="output file path (single task only)")
    p.add_argument("--from-dir", metavar="DIR",
                   help="load one task per .md file in DIR")
    p.add_argument("--suffix", default=".md",
                   help="file suffix for --from-dir (default: .md)")


def _add_runtime_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pipeline", metavar="FILE",
                   help="run a multi-stage pipeline from YAML file")
    p.add_argument("--reuse", action="store_true",
                   help="reuse existing .out.md files (skip regeneration)")
    p.add_argument("--reuse-legacy", action="store_true",
                   help="existence-only reuse check (skip validators; escape hatch for T1 freshness revalidation)")
    p.add_argument("--reuse-fingerprint", action="store_true",
                   help="T9: reuse only when the artifact's sidecar input-fingerprint matches (input change -> regenerate)")
    p.add_argument("--force", action="store_true",
                   help="force regeneration, overwrite existing .out.md (default)")
    p.add_argument("--git-commit", action="store_true",
                   help="auto git commit after each stage (overrides pipeline setting)")
    p.add_argument("--no-git-commit", action="store_true",
                   help="disable git commit (overrides pipeline setting)")
    p.add_argument("--commit-prefix", default=COMMIT_PREFIX_DEFAULT,
                   help=f"prefix for auto-generated commit messages (default: {COMMIT_PREFIX_DEFAULT})")
    p.add_argument("--log-file", default="",
                   help="Append run log to FILE for 24x7 supervision (rotated)")
    p.add_argument("--log-max-bytes", type=int, default=config.LOG_MAX_BYTES,
                   help=f"Rotate the log file after this many bytes (default: {config.LOG_MAX_BYTES})")
    p.add_argument("--log-backups", type=int, default=config.LOG_BACKUP_COUNT,
                   help=f"Keep this many rotated log backups (default: {config.LOG_BACKUP_COUNT})")
    p.add_argument("--decision-log", default="",
                   help="Append structured per-stage decision records to FILE (overrides pipeline 'decision_log')")
    p.add_argument("--archive-dir", default="",
                   help="Move completed deliverables into DIR/<label>-<timestamp>/ after a fully successful run (keeps the worktree clean; git history retains everything)")
    p.add_argument("--session-mode", choices=["new", "shared", "per-stage"], default="new",
                   help="Session reuse: new = fresh session per call (default), shared = one session for the whole batch/pipeline, per-stage = one session per pipeline stage")
    p.add_argument("--session-max-bytes", type=int, default=config.SESSION_MAX_BYTES,
                   help=f"T10: fork the shared session past this size (default: {config.SESSION_MAX_BYTES})")
    p.add_argument("--session-name", default="",
                   help="Reproducible session base name (default: task source file stem); shared sessions continue across runs")
    p.add_argument("--validate", default="",
                   help="Named validators from pi-batch.yaml validators registry, comma-separated (e.g. 'quick,gofmt'); AND semantics. Unknown names are treated as raw shell commands")
    p.add_argument("--validate-cmd", default="",
                   help="Engineering gate run against every agent result BEFORE its output is saved (e.g. 'go build ./... && go vet ./...', 'python cli.py check'); {output} and {cwd} placeholders are substituted. Non-zero exit rejects the result and leaves no file")
    p.add_argument("--no-lock", action="store_true",
                   help="skip the single-instance lock (T4 escape hatch)")
    p.add_argument("--approve-file", default="",
                   help="T12c: file whose existence approves approval: true stages (CI-friendly)")
    p.add_argument("--circuit-max", type=int, default=5,
                   help="T5: isolate a task after this many consecutive failures (default: 5)")
    p.add_argument("--stall-rounds", type=int, default=6,
                   help="T5: stop the loop (exit 4) after this many rounds without progress (default: 6)")
    _add_metering_args(p)
    p.add_argument("--dry-run", action="store_true",
                   help="print task list without executing")
    return p


def _add_batch_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mode", choices=["serial", "parallel"], default="serial",
                   help="execution mode (default: serial)")
    p.add_argument("-w", "--workers", type=int, default=AGENT_DEFAULT_WORKERS,
                   help=f"parallel worker count (default: {AGENT_DEFAULT_WORKERS})")
    p.add_argument("--agent-bin", default=AGENT_BIN,
                   help=f"agent CLI binary to invoke per task (default: {AGENT_BIN}; "
                        "set agent.bin in pi-batch.yaml to change the default)")
    p.add_argument("--model", default="",
                   help="default model override for all tasks")
    p.add_argument("--provider", default="",
                   help="default provider override for all tasks (CLI > task > stage > config)")
    p.add_argument("--timeout", type=int, default=0,
                   help="default timeout override for all tasks (seconds)")
    p.add_argument("--retries", type=int, default=0,
                   help="Retry failed tasks up to N extra attempts with exponential backoff (serial mode)")
    p.add_argument("--retry-delay", type=float, default=10.0,
                   help="Base retry wait in seconds (default: 10; rate-limit/network failures wait at least 30s)")
    p.add_argument("--retry-backoff", type=float, default=2.0,
                   help="Retry backoff multiplier (default: 2)")
    p.add_argument("--min-interval", type=float, default=0.0,
                   help="Minimum seconds between successful serial tasks (throttle for 24x7 runs)")
    p.add_argument("--max-rounds", type=int, default=1,
                   help="Max execution rounds; 0 = loop forever until every task passes (default: 1)")
    p.add_argument("--round-delay", type=float, default=60.0,
                   help="Seconds to wait between rounds (default: 60)")


def _sigterm(signum, frame):
    """S1 (SRE round-4 finding): SIGTERM (systemd stop, operator kill)
    behaves like Ctrl-C — children are killed, the lock released, exit
    130 — instead of stranding agent process groups and the lock."""
    log.warning("\nSIGTERM received; stopping children and releasing the lock (exit 130)")
    raise KeyboardInterrupt()


def main() -> None:
    args = build_parser().parse_args()
    config.AGENT_BIN = args.agent_bin
    if args.session_max_bytes != config.SESSION_MAX_BYTES:
        config.SESSION_MAX_BYTES = args.session_max_bytes

    signal.signal(signal.SIGTERM, _sigterm)

    _wire_metering(args)
    _auto_detect_pipeline(args)

    # Append run log to FILE for 24x7 supervision; T3 (7x24 governance):
    # bounded rotation instead of unbounded growth (disk-full crashes were
    # an SRE finding in the self-review).
    if args.log_file:
        fh = logging.handlers.RotatingFileHandler(
            args.log_file, maxBytes=args.log_max_bytes, backupCount=args.log_backups,
            encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(fh)

    # T4 (7x24 governance): single-instance lock; skipped for --dry-run.
    lock_path = acquire_lock(no_lock=args.no_lock or args.dry_run)

    pipeline = _setup_pipeline(args)
    timeout_override = args.timeout if "--timeout" in sys.argv else 0
    # T11: CLI --provider wins over stage-level providers (precedence D6:
    # CLI > task > stage > config default).
    if args.provider:
        for stage in pipeline.stages:
            stage.provider = args.provider

    session_name = _derive_session_name(args)
    _validate_session_flags(args)

    # Validation gates: named validators (--validate) and the raw command
    # (--validate-cmd) both apply, in that order, with AND semantics.
    cli_validate = ",".join(x for x in (args.validate, args.validate_cmd) if x)

    try:
        _run_rounds(args, pipeline, session_name, cli_validate, timeout_override, lock_path)
    except KeyboardInterrupt:
        log.warning("\nInterrupted by user. Rerun with --reuse to continue later.")
        sys.exit(130)
    finally:
        release_lock(lock_path)


def _validate_session_flags(args) -> None:
    """Shared/per-stage sessions require serial execution; per-stage needs
    a pipeline (single-batch has no stages)."""
    if args.session_mode != "new" and args.mode == "parallel" and not args.pipeline:
        log.error("--session-mode %s requires --mode serial (parallel would interleave one session)", args.session_mode)
        sys.exit(1)
    if args.session_mode == "per-stage" and not args.pipeline:
        log.error("--session-mode per-stage requires --pipeline (there are no stages in single-batch mode)")
        sys.exit(1)


def _verify_round_lock(lock_path: Optional[Path]) -> None:
    """T4 (round-4 DS-1): re-verify the instance lock between rounds — a
    lock that vanished (unlinked by a concurrent breaker) means the
    single-instance guarantee is gone; continue would be split-brain."""
    if lock_path and not verify_held(lock_path):
        log.error("LOCK LOST: %s is no longer held by this instance (removed by another process?); "
                  "stopping to avoid split-brain (exit %d)", lock_path, LOCK_EXIT_CODE)
        sys.exit(LOCK_EXIT_CODE)


def _warn_round_residue(pipeline: Optional[Pipeline]) -> None:
    """T5: warn about residue markers left by a killed run. Markers are
    written under each task's cwd, so cwd-aware tasks' orphans are scanned
    in their own dirs too (round-4 L6)."""
    if pipeline is not None:
        dirs = [os.getcwd()] + [s.from_dir for s in pipeline.stages if s.from_dir]
        _warn_residue_markers(dirs)
    else:
        _warn_residue_markers()


def _run_rounds(args, pipeline: Optional[Pipeline], session_name: str, cli_validate: str, timeout_override: int,
                lock_path: Optional[Path] = None) -> None:
    """The 24x7 round loop: rerun only the tasks/stages that failed or were
    rejected each round; --max-rounds 0 loops forever with --round-delay
    rest between rounds so quota and rate-limit windows can clear.

    T5 (7x24 governance): circuit isolation, stall detection, residue
    markers and environment-class halts keep an unattended run from
    burning quota against a broken provider or poisoned task set.
    """
    max_rounds = args.max_rounds
    # F9: stdin is consumed ONCE before the round loop; later rounds reuse
    # the loaded tasks (an exhausted stdin must not kill round 2+).
    stdin_tasks = None
    if pipeline is None and not (args.from_dir or args.prompt or args.source):
        stdin = sys.stdin.read().strip()
        if stdin:
            stdin_tasks = [Task(prompt=stdin)]
    round_no = 0
    circuit = Circuit(max_failures=args.circuit_max)
    stall = StallDetector(k=args.stall_rounds)
    while True:
        round_no += 1
        log.info("")
        log.info("=" * 60)
        log.info("ROUND %d of %s", round_no, "unlimited" if max_rounds == 0 else max_rounds)
        log.info("=" * 60)

        _verify_round_lock(lock_path)
        _warn_round_residue(pipeline)
        round_failed, failed_keys = _execute_round(args, pipeline, stdin_tasks, session_name,
                                                   cli_validate, timeout_override, circuit, round_no)

        if not round_failed:
            log.info("All tasks passed in round %d", round_no)
            return
        if _govern_round(max_rounds, round_no, pipeline, circuit, failed_keys):
            sys.exit(1)
        if _circuit_all_stopped(circuit, failed_keys):
            sys.exit(STALL_EXIT_CODE)
        if stall.update(failed_keys, 0):
            sys.exit(STALL_EXIT_CODE)
        log.warning("Round %d finished with failures; waiting %.0fs before round %d",
                    round_no, args.round_delay, round_no + 1)
        time.sleep(args.round_delay)


def _govern_round(max_rounds: int, round_no: int, pipeline, circuit: Circuit, failed_keys: set) -> bool:
    """T5: round-bound governance — max-rounds exhaustion and pipeline
    circuit feeding; True when the loop must exit."""
    if max_rounds > 0 and round_no >= max_rounds:
        log.error("Max rounds (%d) reached with tasks still failing; rerun with --reuse to continue later", max_rounds)
        return True
    if pipeline is not None:
        # a poison stage must not spin the loop forever (round-4 P0-1)
        for key in failed_keys:
            circuit.register_failure(key, "pipeline round failure")
    return False


def _execute_round(args, pipeline: Optional[Pipeline], stdin_tasks, session_name: str, cli_validate: str,
                   timeout_override: int, circuit: Circuit, round_no: int) -> tuple[bool, set]:
    """Run one round (pipeline or single-batch) and return
    (round_failed, failed_keys) for the T5 governance checks."""
    if pipeline is not None:
        return _run_pipeline_round(args, pipeline, session_name, cli_validate,
                                   timeout_override, reuse_outputs=args.reuse and not args.force)
    return _run_batch_round(args, stdin_tasks, session_name, cli_validate,
                            args.max_rounds, round_no, circuit)


def _warn_residue_markers(dirs: Optional[list] = None) -> None:
    """T5: start markers surviving a previous (killed) run are warned about;
    the current round regenerates them instead of trusting them silently.
    Markers are written under the TASK cwd, so cwd-aware tasks' orphans must
    be scanned in their own dirs, not just the process cwd (round-4 L6)."""
    for d in dict.fromkeys(dirs or [os.getcwd()]):
        for r in residue_markers(d):
            log.warning("RESIDUE: stale start marker %s in %s (previous run was killed); regenerating", r.name, d)


def _circuit_all_stopped(circuit: Circuit, failed_keys: set) -> bool:
    """True when every failing task is circuit-isolated (nothing left to
    run — a poison task set must not spin the loop forever)."""
    if not circuit.isolated:
        return False
    log.warning("CIRCUIT: %d task(s) isolated after repeated failures: %s",
                len(circuit.isolated), ", ".join(sorted(circuit.isolated)))
    if not failed_keys - circuit.isolated:
        log.error("All failing tasks are isolated; stopping (exit %d)", STALL_EXIT_CODE)
        return True
    return False


def _add_metering_args(p) -> None:
    """T7/T8: metering events, webhook, and invocation budgets."""
    p.add_argument("--events-file", default="",
                   help="T7: append JSONL metering events (task_finish/task_fail/gate_reject/budget_cap/stall_stop)")
    p.add_argument("--webhook", default="",
                   help="T7: POST each metering event to URL (non-blocking, 5s timeout, failures logged only)")
    p.add_argument("--budget-max", type=int, default=0,
                   help="T8: cap agent invocations per run (0 = unlimited); on cap stop with exit 3")
    p.add_argument("--daily-budget", type=int, default=0,
                   help="T8: cap agent invocations per UTC day (0 = unlimited); state in --daily-state file")
    p.add_argument("--daily-state", default="",
                   help="T8: daily budget counter state file (default: .pi-batch/daily-budget.json)")


def _wire_metering(args) -> None:
    """T7/T8: metering + budget wiring (module-level, read by run_task)."""
    from . import metering
    metering.EVENTS_FILE = args.events_file
    metering.WEBHOOK_URL = args.webhook
    metering.BUDGET_MAX = args.budget_max
    metering.DAILY_BUDGET = args.daily_budget
    metering._DAILY_STATE = args.daily_state or str(Path(".pi-batch") / "daily-budget.json")


def _auto_detect_pipeline(args) -> None:
    """A YAML source with a top-level 'stages' key is a pipeline definition,
    not a task list, so `pi-batch.py pipeline.yaml --reuse` works without the
    --pipeline flag."""
    if not (args.source and not args.pipeline and yaml):
        return
    src = Path(args.source)
    if src.exists() and src.suffix in (".yaml", ".yml"):
        try:
            data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        if isinstance(data, dict) and "stages" in data:
            log.info("Detected pipeline file (top-level 'stages'): switching to pipeline mode")
            args.pipeline = args.source
            args.source = ""


def _setup_pipeline(args) -> Optional[Pipeline]:
    """Load the pipeline (one-time) and apply CLI overrides: git_commit,
    mode/workers (explicit flags only, scanned from argv because argparse
    cannot report defaults), and the commit-message prefix."""
    if not args.pipeline:
        return None
    pipeline = load_pipeline(args.pipeline)

    if args.git_commit and not args.no_git_commit:
        for stage in pipeline.stages:
            stage.git_commit = True
    elif args.no_git_commit:
        for stage in pipeline.stages:
            stage.git_commit = False

    if "--mode" in sys.argv:
        for stage in pipeline.stages:
            stage.mode = args.mode
    if "-w" in sys.argv or "--workers" in sys.argv:
        for stage in pipeline.stages:
            stage.workers = args.workers

    for stage in pipeline.stages:
        if stage.git_commit and not stage.commit_message:
            stage.commit_message = "%s Stage: %s" % (args.commit_prefix, stage.name)
    return pipeline


def _derive_session_name(args) -> str:
    """Reproducible session base name: explicit --session-name wins, else the
    source stem (pipeline/source/dir), else 'batch'."""
    if args.session_name:
        return args.session_name
    if args.pipeline:
        return Path(args.pipeline).stem
    if args.source:
        return Path(args.source).stem
    if args.from_dir:
        return Path(args.from_dir).name
    return "batch"


def _load_batch_tasks(args, stdin_tasks) -> list:
    """Single-batch task sources: --from-dir files, -p prompt, task file,
    or stdin (consumed once, reused by later rounds)."""
    if args.from_dir:
        tasks = load_tasks_from_dir(args.from_dir, args.suffix)
        if not tasks:
            log.error("No %s files found in %s", args.suffix, args.from_dir)
            sys.exit(1)
        return tasks
    if args.prompt:
        return [Task(prompt=args.prompt, output=args.output or "")]
    if args.source:
        return load_tasks(args.source)
    if stdin_tasks is not None:
        return stdin_tasks
    stdin = sys.stdin.read().strip()
    if stdin:
        return [Task(prompt=stdin)]
    log.error("Provide a prompt (-p), a task file, --from-dir, or --pipeline")
    sys.exit(1)


def _apply_task_overrides(args, tasks) -> None:
    """Single-task output shortcut and global model/timeout overrides."""
    if args.output and len(tasks) == 1:
        tasks[0].output = args.output
    if args.model:
        for t in tasks:
            t.model = args.model
    if args.provider:
        for t in tasks:
            t.provider = args.provider
    if args.timeout:
        for t in tasks:
            t.timeout = args.timeout


def _run_pipeline_round(args, pipeline: Pipeline, session_name: str, cli_validate: str,
                        timeout_override: int, reuse_outputs: bool) -> tuple[bool, set]:
    """One round of pipeline mode; returns (round_failed, failed_keys).
    failed_keys drives the T5 circuit/stall governance in the 24x7 loop:
    failed task keys plus a synthetic 'stage:<name>' key for stages that
    failed without a failed task (e.g. a gate verdict FAIL). Previously the
    pipeline branch crashed with UnboundLocalError and never fed the
    circuit (round-4 finding P0-1/H4/DS-10)."""
    if args.dry_run:
        run_pipeline(pipeline, model_override=args.model, dry_run=True, reuse=reuse_outputs)
        return False, set()
    all_results, failed_stages = run_pipeline(pipeline, model_override=args.model, reuse=reuse_outputs, timeout_override=timeout_override,
                                              session_mode=args.session_mode, session_name=session_name, validate_cmd=cli_validate,
                                              decision_log=args.decision_log, archive_dir=args.archive_dir,
                                              reuse_legacy=args.reuse_legacy, fingerprint_mode=args.reuse_fingerprint,
                                              approval_file=args.approve_file)
    print_summary(all_results)
    if failed_stages or any(not r.success for r in all_results):
        failed_keys = {task_key(r.task) for r in all_results if not r.success}
        for s in failed_stages:
            failed_keys.add("stage:" + s)
        log.error("Failed stages: %s", ", ".join(failed_stages) if failed_stages else "(task failures)")
        return True, failed_keys
    return False, set()


def _run_batch_round(args, stdin_tasks, session_name: str, cli_validate: str, max_rounds: int, round_no: int,
                     circuit: Circuit = None) -> tuple[bool, set]:
    """One round of single-batch mode: load tasks, apply overrides and reuse
    filtering, execute, record decisions and archive on full success.
    Returns True when the round failed (caller may loop again)."""
    tasks = _load_batch_tasks(args, stdin_tasks)
    _apply_task_overrides(args, tasks)

    # T5: residue markers are written under each task's cwd; orphan markers
    # from a killed run must be warned about there too (round-4 finding L6).
    for d in {t.workdir() for t in tasks if t.cwd}:
        _warn_residue_markers([d])

    if not tasks:
        log.error("No tasks to execute: a YAML source must contain a 'tasks' list (pipelines use 'stages' and are auto-detected)")
        sys.exit(1)

    tasks = _drop_isolated(tasks, circuit, round_no)
    tasks = _filter_reused(tasks, args.reuse and not args.force, cli_validate, args.reuse_legacy,
                           args.reuse_fingerprint)
    if not tasks:
        log.info("All tasks already have outputs; nothing to run")
        return False, set()

    if args.dry_run:
        _print_dry_run(tasks, args.mode)
        return False, set()

    results, round_failed = _execute_batch(tasks, args, session_name, cli_validate)
    _finalize_batch_round(args, results, round_failed)
    if args.reuse_fingerprint:
        _write_sidecars(args, tasks, results, cli_validate)
    failed_keys = {task_key(r.task) for r in results if not r.success}
    if circuit is not None:
        for r in results:
            if not r.success:
                circuit.register_failure(task_key(r.task), r.reason or "")
    # T5: identical validator stderr across >=2 tasks = environment-class
    # failure (broken gate, not per-task); halt instead of retrying.
    if round_failed and env_signature(results):
        log.error("ENVIRONMENT: identical validator failure across >=2 tasks; "
                  "stopping loop (exit %d)", STALL_EXIT_CODE)
        sys.exit(STALL_EXIT_CODE)
    return round_failed, failed_keys


def _drop_isolated(tasks: list, circuit: Circuit, round_no: int) -> list:
    """T5: circuit-isolated tasks are dropped from this round (a poison
    task must not burn quota forever)."""
    if circuit is None or not circuit.isolated:
        return tasks
    before = len(tasks)
    kept = [t for t in tasks if circuit.allowed(task_key(t))]
    if len(kept) < before:
        log.warning("CIRCUIT: dropped %d isolated task(s) from round %d", before - len(kept), round_no)
    return kept


def _write_sidecars(args, tasks: list, results: list, cli_validate: str) -> None:
    """T9: persist input fingerprints for successful artifacts."""
    for t, r in zip(tasks, results):
        if r.success and t.output:
            eff = t.validate if t.validate is not None else cli_validate
            write_sidecar(str(t.output_path()), t.prompt, eff, t.model, t.provider)


def _git_commit_single_batch(args, outputs: list) -> None:
    """Best-effort git commit for a single-batch round; failures are logged
    with the real exit codes (never silently reported as success)."""
    if not outputs:
        return
    try:
        if subprocess.run(["git", "rev-parse", "--git-dir"],
                          capture_output=True, timeout=5).returncode != 0:
            log.warning("Not a git repository, skipping git commit")
            return
        add = subprocess.run(["git", "add"] + outputs,
                             capture_output=True, text=True, timeout=10)
        msg = "%s Single batch: %d tasks" % (args.commit_prefix, len(outputs))
        commit = subprocess.run(["git", "commit", "-m", msg],
                                capture_output=True, text=True, timeout=10)
        if add.returncode != 0 or commit.returncode != 0:
            detail = (add.stderr or "") + (commit.stderr or "")
            log.warning("git commit failed (add=%d, commit=%d): %s",
                        add.returncode, commit.returncode, detail.strip()[:500])
        else:
            log.info("GIT COMMIT: %s (files: %d)", msg, len(outputs))
    except Exception as e:
        log.warning("Git commit skipped: %s", e)


def _finalize_batch_round(args, results: list, round_failed: bool) -> None:
    """Decision log, git commit, and archive for a finished single-batch
    round (archive only when the round fully succeeded)."""
    # Structured decision record for single-batch runs (rolling
    # re-analysis keeps a history of what each round decided and why,
    # instead of overwriting the artifact silently)
    if args.decision_log and results:
        source_name = Path(args.source).stem if args.source else (args.from_dir or "batch")
        _append_decision_log(args.decision_log, source_name, results, not round_failed, None)

    # Git commit for single-batch modes (per round)
    if args.git_commit and not args.no_git_commit:
        # Local fix (self-iteration round 2, P0-1): resolved paths only.
        outputs = [str(r.task.output_path()) for r in results if r.success and r.task.output]
        _git_commit_single_batch(args, outputs)

    # Rolling re-analysis produces many artifacts; archive them once a
    # round fully succeeded so the worktree stays clean.
    if not round_failed and args.archive_dir and results:
        outputs = [r.task.output for r in results if r.success and r.task.output]
        _archive_outputs(outputs, args.archive_dir, "batch")



def _filter_reused(tasks: list, reuse: bool, validate_cmd: str = "", reuse_legacy: bool = False,
                   fingerprint_mode: bool = False) -> list:
    """Drop tasks whose output already exists when reuse is on, so a later
    round reruns only the failures. T1: a task is reused only when its
    artifact passes reuse_decision (exists, non-empty, not a symlink, and
    every effective validator still passes); stale artifacts are deleted so
    the round regenerates them. --reuse-legacy restores existence-only."""
    if not reuse:
        return tasks
    kept = []
    for t in tasks:
        if not t.output:
            kept.append(t)
            continue
        effective = t.validate if t.validate is not None else validate_cmd
        fp = reuse_fingerprint(t, effective) if fingerprint_mode else None
        if reuse_decision(str(t.output_path()), effective, t.workdir(), reuse_legacy, fp):
            continue  # reused
        kept.append(t)
    skipped = len(tasks) - len(kept)
    if skipped:
        log.info("Reuse: %d task(s) reused+validated, skipped", skipped)
    return kept


def _print_dry_run(tasks: list, mode: str) -> None:
    """Print what single-batch would execute without running it."""
    print("Tasks: %d" % len(tasks))
    print("Mode:  %s" % mode)
    print()
    for i, t in enumerate(tasks, 1):
        print("  [%d] %s..." % (i, t.prompt[:80]))
        print("      model=%s  dir=%s  output=%s" %
              (t.model or "default", t.workdir(), t.output or "(stdout)"))


def _execute_batch(tasks: list, args, session_name: str, cli_validate: str) -> tuple[list, bool]:
    """Run the single-batch tasks (serial with retries, or parallel) and
    print the summary. Returns (results, round_failed)."""
    if args.mode == "serial":
        results = run_serial(tasks, retries=args.retries, retry_delay=args.retry_delay,
                             backoff=args.retry_backoff, min_interval=args.min_interval,
                             session_mode=args.session_mode, session_id=session_name, session_name=session_name,
                             validate_cmd=cli_validate)
    else:
        results = run_parallel(tasks, args.workers, validate_cmd=cli_validate)
    print_summary(results)
    return results, any(not r.success for r in results)


