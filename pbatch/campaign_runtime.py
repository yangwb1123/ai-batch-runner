"""Process lifecycle helpers for isolated Campaign pipelines."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import config, metering
from .campaign_models import Direction
from .campaign_state import git_worktree_evidence
from .campaign_worktree import finalize_worktree, isolated_status
from .launcher import cli_argv, is_frozen
from .pipeline_status import PipelineStatus
from .text_io import read_text_bounded


def _agent_args(args: argparse.Namespace) -> list[str]:
    values = ["--agent-bin", args.agent_bin, "--memory-mode", args.memory_mode,
              "--stream-output", args.stream_output]
    for flag, value in (("--model", args.model), ("--provider", args.provider),
                        ("--thinking", getattr(args, "thinking", "")),
                        ("--timeout", args.timeout)):
        if value:
            values.extend([flag, str(value)])
    return values


def _reuse_enabled(args: argparse.Namespace) -> bool:
    return not args.force and (args.reuse or args.skip_passed or args.retry_failed)


def _print_plan(context, modules: list[str]) -> None:
    analysis_sec = context.store.median_elapsed(
        ("ANALYZED",), context.settings.analysis_minutes * 60)
    pipeline_sec = context.store.median_elapsed(
        (PipelineStatus.PASSED, PipelineStatus.GATE_REJECTED,
         PipelineStatus.PIPELINE_FAILED, PipelineStatus.VALIDATION_FAILED,
         PipelineStatus.PROVIDER_BREAKER_PAUSED),
        context.settings.pipeline_minutes * 60)
    analysis_wall = len(modules) * analysis_sec / context.settings.analysis_jobs
    directions = (context.settings.max_total_directions or
                  len(modules) * context.settings.max_directions)
    pipeline_wall = directions * pipeline_sec / context.settings.pipeline_concurrency
    print(f"== Campaign: {context.settings.name} ==")
    print(f"Modules: {len(modules)}; directions/module: {context.settings.max_directions}; "
          f"global directions: {context.settings.max_total_directions or 'per-module'}")
    for module in modules:
        print(f"  - {module}")
    print(f"Estimated analysis: {analysis_wall / 60:.0f} min")
    print(f"Estimated pipelines: {pipeline_wall / 60:.0f} min")
    print(f"Estimated total: {(analysis_wall + pipeline_wall) / 60:.0f} min")
    print("Dry-run made no files or state changes.")


_ACTIVE_PIPELINES: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()


def isolated_command(worktree: Path, pipeline_path: Path, logfile: Path,
                     agent_args: list[str]) -> list[str]:
    local_entrypoint = worktree / "pi-batch.py"
    tool_entrypoint = Path(config.TOOL_ROOT) / "pi-batch.py"
    # Materialized campaign pipelines use the current runner schema. Prefer
    # it over an older project-local vendored shim while retaining the target
    # worktree as cwd for project config and assets.
    entrypoint = tool_entrypoint if tool_entrypoint.is_file() else local_entrypoint
    runner = cli_argv(entrypoint=entrypoint)
    cmd = [*runner, str(pipeline_path), "--reuse", "--reuse-fingerprint",
           "--log-file", str(logfile)]
    if metering.EVENTS_FILE:
        cmd.extend(["--events-file", metering.EVENTS_FILE])
    # Isolated children start a fresh process; the prompt-overflow policy is
    # process config and must be passed explicitly or the child falls back to
    # the fail-closed default.
    if config.PROMPT_OVERFLOW and config.PROMPT_OVERFLOW != "fail":
        cmd.extend(["--prompt-overflow", config.PROMPT_OVERFLOW])
    # Older project-local shims may lack --run-id; coordinator outcomes remain
    # authoritative while those children create their own metering run ids.
    if metering.RUN_ID and (is_frozen() or entrypoint == tool_entrypoint):
        cmd.extend(["--run-id", metering.RUN_ID])
    return [*cmd, *agent_args]


def isolated_failure_reason(settings, returncode: int, timed_out: bool, stalled: bool) -> str:
    if stalled:
        return f"isolated pipeline made no progress for {settings.pipeline_no_progress_timeout}s"
    if timed_out:
        return f"isolated pipeline timed out after {settings.pipeline_timeout}s"
    return "" if returncode == 0 else f"isolated pipeline exited {returncode}"


def copy_local_analysis(worktree: Path, direction: Direction,
                        analysis_path: Path) -> Path:
    target = config.data_dir(str(worktree)) / "campaign" / f"{direction.direction_id}-analysis.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(read_text_bounded(
        analysis_path, config.OUTPUT_MAX_BYTES, "campaign analysis"),
        encoding="utf-8")
    return target


def run_isolated_child(worktree: Path, pipeline_path: Path, logfile: Path,
                       agent_args: list[str], timeout: int) -> tuple[int, bool]:
    local_entrypoint = worktree / "pi-batch.py"
    entrypoint = (local_entrypoint if local_entrypoint.is_file()
                  else Path(config.TOOL_ROOT) / "pi-batch.py")
    runner = cli_argv(entrypoint=entrypoint)
    command = [*runner, str(pipeline_path), "--reuse", "--reuse-fingerprint",
               "--log-file", str(logfile)]
    if config.PROMPT_OVERFLOW and config.PROMPT_OVERFLOW != "fail":
        command.extend(["--prompt-overflow", config.PROMPT_OVERFLOW])
    command.extend(agent_args)
    proc = subprocess.Popen(command, cwd=worktree, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    with _ACTIVE_LOCK:
        _ACTIVE_PIPELINES.add(proc)
    try:
        return _wait_isolated_child(proc, timeout)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_PIPELINES.discard(proc)


def _wait_isolated_child(proc: subprocess.Popen, timeout: int) -> tuple[int, bool]:
    try:
        return proc.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        kill_pipeline_group(proc)
        return -1, True


def isolated_result(worktree: Path, branch: str, baseline: str,
                    direction: Direction, returncode: int, timed_out: bool,
                    logfile: Path, timeout: int) -> tuple[str, str, dict]:
    status = (PipelineStatus.PIPELINE_FAILED if timed_out
              else isolated_status(returncode, logfile))
    reason = (f"isolated pipeline timed out after {timeout}s"
              if timed_out else
              ("" if returncode == 0 else f"isolated pipeline exited {returncode}"))
    repository = git_worktree_evidence(worktree)
    if returncode == 0 and not timed_out:
        reason, repository = finalize_worktree(
            worktree, branch, baseline, direction.direction_id)
        if reason:
            status = PipelineStatus.PIPELINE_FAILED
    return status, reason, repository


def kill_pipeline_group(proc: subprocess.Popen) -> None:
    """Terminate an isolated pipeline and reap its direct child."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def kill_isolated_pipelines() -> None:
    """Kill active worktree pipeline process groups during interruption."""
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE_PIPELINES)
    for proc in processes:
        if proc.poll() is not None:
            continue
        kill_pipeline_group(proc)
