#!/usr/bin/env python3
"""pi-batch -- serial/parallel batch executor for a CLI coding agent.

The agent binary (default: `pi`) and other defaults are declared in
pi-batch.yaml, not hardcoded -- copy pi-batch.py + pi-batch.yaml + the
pi_batch_*.py helper modules into another project and edit `agent.bin` to
point at a different agent CLI (claude, codex, gemini, opencode, ...); see
pi-batch.yaml's header comment.

Split across pi-batch.py (CLI entry) + pi_batch_config.py (config/logging)
+ pi_batch_task.py (task model + serial/parallel engine) +
pi_batch_pipeline.py (--pipeline mode) per CLAUDE.md's 500-line file gate --
the four files must be copied together, not just this one.

Usage:
  # From YAML task file
  python pi-batch.py tasks.yaml

  # Single task via CLI
  python pi-batch.py -p "analyze this project" -o output.md

  # Parallel execution
  python pi-batch.py tasks.yaml --mode parallel --workers 4

  # Serial execution (default)
  python pi-batch.py tasks.yaml --mode serial

Example tasks.yaml:
  ---
  tasks:
    - prompt: "Analyze the project expansion directions"
      output: docs/expansion.md
      model: claude-sonnet
      cwd: /home/dwp/snaplink

    - prompt: "Review security edge cases"
      output: docs/security-review.md
      model: claude-sonnet:high
      cwd: /home/dwp/snaplink

    - prompt: |
        Based on the current codebase, list performance bottlenecks
      output: docs/perf.md
      model: claude-haiku
      cwd: /home/dwp/snaplink
"""

from __future__ import annotations

import argparse
import sys

import pi_batch_config as cfg
from pi_batch_pipeline import load_pipeline, run_pipeline
from pi_batch_task import Task, load_tasks, load_tasks_from_dir, run_parallel, run_serial

log = cfg.log


# -- summary report ---------------------------------------------------
def print_summary(results) -> None:
    """Print an execution summary table."""
    total = len(results)
    succeeded = sum(1 for r in results if r.success)
    failed = total - succeeded
    total_elapsed = sum(r.elapsed for r in results)
    wall_time = max(r.elapsed for r in results) if results else 0

    print()
    print("=" * 56)
    print("  pi-batch execution report")
    print("=" * 56)
    print("  total:     %d" % total)
    print("  succeeded: %d" % succeeded)
    print("  failed:    %d" % failed)
    print("  CPU time:  %.1fs" % total_elapsed)
    print("  wall time: %.1fs" % wall_time)
    print()
    for r in results:
        icon = "PASS" if r.success else "FAIL"
        brief = r.task.prompt[:60].replace("\n", " ")
        print("  %s  [%6.1fs] %s..." % (icon, r.elapsed, brief))
    print("=" * 56)


# -- CLI ---------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="pi-batch -- serial/parallel batch executor for pi agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("source", nargs="?",
                   help="YAML task file / JSON file / plain text prompt")
    p.add_argument("-p", "--prompt", help="inline prompt (single task shortcut)")
    p.add_argument("-o", "--output", help="output file path (single task only)")
    p.add_argument("--mode", choices=["serial", "parallel"], default="serial",
                   help="execution mode (default: serial)")
    p.add_argument("-w", "--workers", type=int, default=cfg.AGENT_DEFAULT_WORKERS,
                   help=f"parallel worker count (default: {cfg.AGENT_DEFAULT_WORKERS})")
    p.add_argument("--agent-bin", default=cfg.AGENT_BIN,
                   help=f"agent CLI binary to invoke per task (default: {cfg.AGENT_BIN}; "
                        "set agent.bin in pi-batch.yaml to change the default)")
    p.add_argument("--model", default="",
                   help="default model override for all tasks")
    p.add_argument("--timeout", type=int, default=0,
                   help="default timeout override for all tasks (seconds)")
    p.add_argument("--from-dir", metavar="DIR",
                   help="load one task per .md file in DIR")
    p.add_argument("--suffix", default=".md",
                   help="file suffix for --from-dir (default: .md)")
    p.add_argument("--pipeline", metavar="FILE",
                   help="run a multi-stage pipeline from YAML file")
    p.add_argument("--reuse", action="store_true",
                   help="reuse existing .out.md files (skip regeneration)")
    p.add_argument("--force", action="store_true",
                   help="force regeneration, overwrite existing .out.md (default)")
    p.add_argument("--git-commit", action="store_true",
                   help="auto git commit after each stage (overrides pipeline setting)")
    p.add_argument("--no-git-commit", action="store_true",
                   help="disable git commit (overrides pipeline setting)")
    p.add_argument("--commit-prefix", default=cfg.COMMIT_PREFIX_DEFAULT,
                   help=f"prefix for auto-generated commit messages (default: {cfg.COMMIT_PREFIX_DEFAULT})")
    p.add_argument("--dry-run", action="store_true",
                   help="print task list without executing")
    return p


def _run_pipeline_mode(args) -> None:
    """Handle --pipeline: load it, apply CLI overrides, then run (or dry-run) it."""
    pipeline = load_pipeline(args.pipeline)
    reuse_outputs = args.reuse and not args.force

    # Apply git_commit override to all stages
    if args.git_commit and not args.no_git_commit:
        for stage in pipeline.stages:
            stage.git_commit = True
    elif args.no_git_commit:
        for stage in pipeline.stages:
            stage.git_commit = False

    # Apply commit message prefix
    for stage in pipeline.stages:
        if stage.git_commit and not stage.commit_message:
            stage.commit_message = "%s Stage: %s" % (args.commit_prefix, stage.name)

    if args.dry_run:
        run_pipeline(pipeline, model_override=args.model, dry_run=True, reuse=reuse_outputs)
        return

    try:
        all_results = run_pipeline(pipeline, model_override=args.model, reuse=reuse_outputs)
        print_summary(all_results)
        if any(not r.success for r in all_results):
            sys.exit(1)
    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        sys.exit(130)


def _load_single_stage_tasks(args) -> list[Task]:
    """Load tasks for non-pipeline modes: --from-dir / -p / source file / stdin."""
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

    stdin = sys.stdin.read().strip()
    if stdin:
        return [Task(prompt=stdin)]
    log.error("Provide a prompt (-p), a task file, --from-dir, or --pipeline")
    sys.exit(1)


def _git_commit_single_stage(args, results) -> None:
    """Auto git-commit outputs for non-pipeline modes (best-effort, never raises)."""
    import subprocess
    outputs = [r.task.output for r in results if r.success and r.task.output]
    if not outputs:
        return
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, timeout=5)
        subprocess.run(["git", "add"] + outputs, capture_output=True, timeout=10)
        msg = "%s Single batch: %d tasks" % (args.commit_prefix, len(outputs))
        subprocess.run(["git", "commit", "-m", msg], capture_output=True, timeout=10)
        log.info("GIT COMMIT: %s (files: %d)", msg, len(outputs))
    except Exception as e:
        log.warning("Git commit skipped: %s", e)


def _apply_task_overrides(args, tasks: list[Task]) -> None:
    """Apply -o/--model/--timeout CLI overrides to the loaded task list, in place."""
    if args.output and len(tasks) == 1:
        tasks[0].output = args.output
    if args.model:
        for t in tasks:
            t.model = args.model
    if args.timeout:
        for t in tasks:
            t.timeout = args.timeout


def _print_dry_run(args, tasks: list[Task]) -> None:
    """Print the resolved task list for --dry-run, without executing anything."""
    print("Tasks: %d" % len(tasks))
    print("Mode:  %s" % args.mode)
    print()
    for i, t in enumerate(tasks, 1):
        print("  [%d] %s..." % (i, t.prompt[:80]))
        print("      model=%s  dir=%s  output=%s" %
              (t.model or "default", t.workdir(), t.output or "(stdout)"))


def main() -> None:
    args = build_parser().parse_args()
    cfg.AGENT_BIN = args.agent_bin

    if args.pipeline:
        _run_pipeline_mode(args)
        return

    tasks = _load_single_stage_tasks(args)
    _apply_task_overrides(args, tasks)

    if not tasks:
        log.error("No tasks to execute")
        sys.exit(1)

    # -- dry-run --
    if args.dry_run:
        _print_dry_run(args, tasks)
        return

    # -- execute --
    try:
        if args.mode == "serial":
            results = run_serial(tasks)
        else:
            results = run_parallel(tasks, args.workers)

        print_summary(results)

        # Git commit for single-stage modes
        if not args.pipeline and args.git_commit and not args.no_git_commit:
            _git_commit_single_stage(args, results)

        if any(not r.success for r in results):
            sys.exit(1)

    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
