"""Task execution with hard deadlines, retries, gates, and summaries."""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional
from .validation_batch import run_file_validators
from . import config
from .config import _resolve_validator_specs, _resolve_validators, _session_flags, log
from .cmd_expand import embedded_checker_argv, expand_cmd
from .context_inject import inject_task_context
from .execution_trace import (annotate_result, attach_session,
                              finish_task_result as _finish_result,
                              record_validation_event)
from .models import Task, TaskResult
from .execution_ids import task_id as make_task_id
from .runner_results import (agent_result_reason as _agent_result_reason,
                             agent_stdout_banner_reason as _stdout_banner_reason,
                             expects_codex_transcript as _expects_codex_transcript,
                             overflow_result as _overflow_result,
                             projected_agent_stdout as _projected_agent_stdout,
                             prompt_limit_result as _prompt_limit_result)
from .provider_breaker import (cooldown_failure as _pb_cooldown,
                               provider_identity as _pb_identity,
                               provider_failure_reason as _pb_reason,
                               register_provider_failure as _pb_register_fail,
                               register_success as _pb_register_ok)
from . import metering
from .metering import budget_try_consume, record_event
from .memory import record_task
from .reuse import capture_worktree_state, preserve_failed_worktree
from .runner_session import (bind_spawned_session as _bind_spawned_session,
                             _capture_codex_thread,
                             _memory_invocation,
                             rotation_flags as _rotation_flags,
                             unexpected_result as _unexpected_result)
from .triage import clear_marker, is_retryable, task_key, write_marker
from .process_exec import (ValidationResult, _DrainTimeout, run_bounded,
                           read_chunks, validate_timeout,
                           _kill_group as _kill_command_group)
from .provider_env import provider_child_env
from .process_shutdown import forget_process, interrupt_after_spawn, spawn_registered
from .runner_cancel import (
    CancelledDrain as _CancelledDrain, cancel_requested as _cancel_requested,
    cancelled_result as _cancelled_result,
    join_stream_threads as _join_stream_threads,
    rate_gate as _rate_gate_impl,
    task_cancel_event as _task_cancel_event,
    wait_for_cancel as _wait_for_cancel_impl,
)
from .runner_parallel import run_parallel as _run_parallel_impl
from .runner_process import drain_process as _drain_process_impl
# registry of live agent subprocesses (parallel mode: a budget cap must be
# able to kill in-flight agents so the executor shutdown is prompt)
_ACTIVE_PROCS: set = set()
_ACTIVE_LOCK = threading.RLock()
_PROCESS_POLL_WAIT = threading.Event()
_BUDGET_MESSAGE = ("BUDGET: invocation limit reached; stopping — 调整 "
                   "--max-rounds/limits 后可续跑（已保留现场）")
def _scheduled_sleep(seconds: float) -> None:
    """Wait for runner-level retry/throttle scheduling.

    Keeping this boundary separate from the stdlib ``time`` module lets
    tests replace runner waits without also changing subprocess' timeout
    polling in other threads.
    """
    time.sleep(seconds)
def _register_proc(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.add(proc)
    interrupt_after_spawn()
def _unregister_proc(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCS.discard(proc)
    forget_process(proc)
def proc_snapshot() -> tuple[subprocess.Popen, ...]:
    with _ACTIVE_LOCK:
        return tuple(_ACTIVE_PROCS)
def kill_active_procs() -> None:
    """Kill every currently-running agent process group (T8 parallel cap:
    in-flight agents must not keep the runner waiting after the cap)."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE_PROCS)
    for proc in procs:
        _kill_group(proc)
def _read_stream(stream, prefix: str, collector: list, cap: int = 0,
                 overflow: Optional[list] = None, emit: bool = False,
                 target=None) -> None:
    """Drain and collect a child stream, optionally displaying it live.
    Collection caps at *cap* bytes (T12e); overflow is recorded so the
    caller can reject. The stream is still drained (a blocked pipe would
    hang the agent), while collection/display stop at the cap."""
    total = 0
    sink = target or sys.stdout
    try:
        for line in read_chunks(stream, errors="strict"):
            within_cap = True
            if cap > 0:
                total += len(line.encode("utf-8", errors="replace"))
                within_cap = total <= cap
            if within_cap:
                collector.append(line)
                if emit:
                    sink.write(f"{prefix}{line}" if prefix else line)
                    sink.flush()
            elif overflow is not None:
                overflow[0] = True
    except (OSError, ValueError) as exc:
        if overflow is not None:
            overflow.error = f"agent output read failed: {exc}"
    finally:
        stream.close()
def agent_failure_reason(returncode: int, output: str, bin_name: str = "") -> str:
    """Discard-reason ('' = usable): non-zero exit / empty output /
    provider-signature rejections; per-agent patterns replace the
    built-in table (G line)."""
    if returncode != 0:
        return f"agent exited {returncode}"
    if not output or not output.strip():
        return "agent produced no output"
    for pattern in config.agent_error_patterns(bin_name):
        if pattern.search(output):
            return f"agent reported provider failure ({pattern.pattern})"
    return ""
def _budget_gate(task: Task, key: str, workdir: str, session_name: str = "",
                 trace_task_id: str = "", attempt: int = 0) -> None:
    """Stop before spawn when the atomic parallel-safe budget is exhausted."""
    if not budget_try_consume():
        log.error(_BUDGET_MESSAGE)
        record_event("budget_cap", task.prompt, "invocation limit", workdir,
                     session_name, task_id=trace_task_id,
                     stage_id=str(task.memory.get("stage", "")), attempt=attempt)
        clear_marker(key, workdir)
        raise SystemExit(3)
def _prompt_brief(task: Task, cmd: list) -> str:
    """One-line log summary with the prompt truncated (log-file safe)."""
    brief = " ".join(task.prompt.split())[:80]
    flags = " ".join(config.agent_argument_flags(
        task.effective_agent())["prompt"])
    elided = "..." if len(brief) == 80 else ""
    return f"{cmd[0]} {flags} {brief}{elided}"
def _kill_group(proc: Optional[subprocess.Popen]) -> None:
    """Kill the whole child process group, tolerating an already-dead
    process, then reap the direct child before returning."""
    if proc is None:
        return
    _kill_command_group(proc)
def kill_proc_group(proc: subprocess.Popen) -> None:
    _kill_group(proc)
def _spawn_agent(cmd: list, workdir: str, env: dict) -> subprocess.Popen:
    """Spawn the agent in its own process group (killed on timeout or
    interrupt). stdin is /dev/null: prompts
    arrive via argv (piped stdin prompts are buffered by the CLI first),
    so an inherited pipe would let stdin-hungry agents (e.g. codex exec
    appends piped stdin as a <stdin> block) swallow or hang on batch input."""
    return spawn_registered(lambda: subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, cwd=workdir,
        env=provider_child_env(env), start_new_session=True), _kill_group)
def _agent_not_found(task: Task) -> TaskResult:
    """The agent binary is missing from PATH: a permanent, non-retryable
    failure (never saved as an artifact)."""
    agent = task.effective_agent()
    log.error(
        "'%s' not found in PATH — 请先安装 agent 或在 pi-batch.yaml 配置 "
        "agent.bin 后重试（本次任务未启动，无副作用）",
        agent,
    )
    return TaskResult(task=task, success=False, stderr=f"{agent} not found in PATH",
                      reason="agent binary not found")
def _context_rejection(task: Task) -> Optional[TaskResult]:
    try:
        if not isinstance(task.env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in task.env.items()):
            raise ValueError("task env must map strings to strings")
        inject_task_context(task)
    except ValueError as exc:
        reason = f"context injection rejected: {exc}"
        log.error("REJECTED: %s", reason)
        return TaskResult(task=task, success=False, returncode=-1,
                          stderr=str(exc), reason=reason)
    return None
def _task_invocation(task: Task, session_flags: Optional[list],
                     session_name: str):
    try:
        validate_timeout(task.timeout, "task")
        cmd, workdir, session_id, effective_name = _memory_invocation(
            task, session_flags, session_name)
    except (TypeError, ValueError) as exc:
        reason = f"agent adapter error: {exc}"
        log.error("REJECTED: %s", reason)
        return TaskResult(task=task, success=False, returncode=-1,
                          stderr=str(exc), reason=reason)
    if prompt_limit := _prompt_limit_result(task, cmd):
        return attach_session(prompt_limit, session_id, effective_name,
                              workdir, task.effective_agent())
    return cmd, workdir, session_id, effective_name
def _stream_task(proc, prefix: str, start: float, timeout: int, emit: bool,
                 stdout_target=None, stderr_target=None, stderr_emit=None,
                 cancel_event=None):
    """Forward opt-in display targets without changing legacy mock calls."""
    targets = {"stdout_target": stdout_target} if stdout_target else {}
    if stderr_target is not None:
        targets["stderr_target"] = stderr_target
    if stderr_emit is not None:
        targets["stderr_emit"] = stderr_emit
    if cancel_event is not None:
        targets["cancel_event"] = cancel_event
    return _stream_proc(proc, prefix, start, timeout, emit, **targets)
def _finish_spawned_result(result, task, flags, stdout_lines, session_id,
                           name, workdir, usage, trace_id, attempt):
    result = _bind_spawned_session(
        result, task, flags, stdout_lines, name, session_id, workdir)
    return _finish_result(
        result, task, session_id, name, workdir, usage, trace_id, attempt)


def run_task(task: Task, task_index: int = 0, total: int = 0, parallel: bool = False, session_flags: Optional[list] = None, session_name: str = "", attempt: int = 1, trace_task_id: str = "", stdout_target=None, stderr_target=None, cancel_event=None) -> TaskResult:
    if _cancel_requested(cancel_event):
        return _cancelled_result(task)
    trace_task_id = trace_task_id or make_task_id(metering.RUN_ID, task.prompt, task.output, task.workdir(), task_index)
    stage_id = str(task.memory.get("stage", ""))
    if rejected := _context_rejection(task):
        annotate_result(rejected, trace_task_id, stage_id, attempt)
        return rejected
    invocation = _task_invocation(task, session_flags, session_name)
    if isinstance(invocation, TaskResult):
        annotate_result(invocation, trace_task_id, stage_id, attempt)
        return invocation
    cmd, workdir, session_id, effective_name = invocation
    start, proc, completed, stdout_lines = time.monotonic(), None, False, []
    prefix, env, key, usage_before = _prepare_task_run(task, cmd, task_index, total, parallel, workdir, effective_name, session_id, trace_task_id, attempt)
    try:
        if (cooldown := _pb_cooldown(task)) is not None:
            return _finish_result(cooldown, task, session_id, effective_name, workdir, usage_before, trace_task_id, attempt)
        _budget_gate(task, key, workdir, effective_name, trace_task_id, attempt)
        rate_ok = (_rate_gate(task, effective_name) if cancel_event is None else
                   _rate_gate(task, effective_name, cancel_event=cancel_event))
        if rate_ok is False or _cancel_requested(cancel_event):
            return _cancelled_result(task)
        proc = _spawn_agent(cmd, workdir, env)
        _register_proc(proc)
        write_marker(key, workdir, agent_pid=proc.pid)
        stream_requested, structured_stdout = _stream_output_enabled(), _expects_codex_transcript(task)
        stdout_streamed = stream_requested and not structured_stdout
        stdout_lines, stderr_lines, overflow = _stream_task(
            proc, prefix, start, task.timeout, stdout_streamed,
            stdout_target, stderr_target, stream_requested, cancel_event)
        completed = True
        result = (_overflow_result(task, proc, stdout_lines, stderr_lines, start)
                  if overflow[0] else _result_from_proc(
                      task, proc, stdout_lines, stderr_lines,
                      time.monotonic() - start))
        result.streamed_output, result.streamed_stderr = stdout_streamed, stream_requested
        return _finish_spawned_result(result, task, session_flags, stdout_lines,
            session_id, effective_name, workdir, usage_before, trace_task_id, attempt)
    except Exception as exc:
        return _task_exception_result(
            exc, task, proc, start, session_flags, stdout_lines, session_id,
            effective_name, workdir, usage_before, trace_task_id, attempt)
    finally:
        if proc is not None and not completed: _kill_group(proc)
        if proc is not None: _unregister_proc(proc)
        clear_marker(key, workdir)
def _task_exception_result(exc, task, proc, start, session_flags,
                           stdout_lines, session_id, effective_name, workdir,
                           usage_before, trace_task_id, attempt):
    if isinstance(exc, _CancelledDrain):
        return _finish_spawned_result(_cancelled_result(task), task,
            session_flags, exc.stdout_lines, session_id, effective_name,
            workdir, usage_before, trace_task_id, attempt)
    if isinstance(exc, _DrainTimeout):
        return _finish_spawned_result(_timeout_result(task, proc, start), task,
            session_flags, exc.stdout_lines, session_id, effective_name,
            workdir, usage_before, trace_task_id, attempt)
    if isinstance(exc, subprocess.TimeoutExpired):
        return _finish_spawned_result(_timeout_result(task, proc, start), task,
            session_flags, [], session_id, effective_name, workdir,
            usage_before, trace_task_id, attempt)
    if isinstance(exc, FileNotFoundError):
        return _finish_result(_agent_not_found(task), task, session_id,
                              effective_name, workdir, usage_before,
                              trace_task_id, attempt)
    result = _unexpected_result(
        exc, proc, start, task, session_flags, stdout_lines,
        effective_name, session_id, workdir)
    return _finish_result(result, task, session_id, effective_name, workdir,
                          usage_before, trace_task_id, attempt)


def _prepare_task_run(task: Task, cmd: list, task_index: int, total: int, parallel: bool,
                      workdir: str, session_name: str, session_id: str = "", trace_task_id: str = "", attempt: int = 0) -> tuple:
    prefix = f"[task-{task_index}] " if (parallel and total > 1) else ""
    log.info(">>  %s  [model=%s]  [timeout=%ss]  [dir=%s]", _prompt_brief(task, cmd), task.model or "default", task.timeout, workdir)
    env = {**os.environ, **task.env}
    key = task_key(task)
    write_marker(key, workdir)
    usage_ref = session_id or session_name
    usage = metering.session_usage(workdir, usage_ref) if metering.EVENTS_FILE else None
    return prefix, env, key, usage

def _rate_gate(task: Task, session_name: str = "", cancel_event=None) -> bool:
    return _rate_gate_impl(task, cancel_event)

def _output_path_is_symlink(task: Task) -> bool:
    """T0.1: probe the UNRESOLVED output path — output_path() resolves
    symlinks away, which would hide a link planted at the output location
    and redirect the write to its target (round-4 finding H1)."""
    probe = Path(task.output)
    if not probe.is_absolute():
        probe = Path(task.workdir()) / probe
    return probe.is_symlink()

def save_result(task: Task, result: TaskResult) -> None:
    """Write a successful task result to its output file, or print to stdout.
    Failed or rejected results are never written to disk: quota or rate-limit
    replies must not become committed artifacts, so the error is logged only.
    """
    out_path = task.output_path()
    if out_path is None:
        structured_failure = (_expects_codex_transcript(task)
                              and not result.success)
        if not result.streamed_output and not structured_failure:
            sys.stdout.write(result.stdout)
        stderr_streamed = (result.streamed_output if result.streamed_stderr is None
                           else result.streamed_stderr)
        if result.stderr and not stderr_streamed:
            sys.stderr.write(result.stderr)
        return

    if not result.success:
        log.error("NOT SAVED %s: task failed (exit=%d, %.1fs)", out_path, result.returncode, result.elapsed)
        return
    # T12e: oversized outputs are rejected, not written (runaway agents
    # must not fill the disk).
    if len(result.stdout.encode("utf-8")) > config.OUTPUT_MAX_BYTES:
        log.error("NOT SAVED %s: output exceeds %d bytes (T12e cap)", out_path, config.OUTPUT_MAX_BYTES)
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # T0.1 (full-SDLC pipeline): refuse symlink targets (a symlinked output
    # path could redirect the write elsewhere) and use a unique mkstemp
    # name, mirroring _save_validated's F14 hardening. Note: probe the
    # UNRESOLVED path — output_path() resolves symlinks away.
    if _output_path_is_symlink(task):
        log.error("NOT SAVED %s: output path is a symlink (refusing to follow)", out_path)
        return
    fd, tmp_name = tempfile.mkstemp(prefix=out_path.name + ".", suffix=".tmp", dir=str(out_path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.write_text(result.stdout, encoding="utf-8")
    tmp.rename(out_path)
    log.info("WROTE %s  (%d bytes)", out_path, len(result.stdout))

_RETRYABLE_REASON = re.compile(r"rate|429|quota|network|connect|unreachable|timeout", re.IGNORECASE)
# P4（使用反馈）：provider 侧模型不可用/过载——快速失败后需要更长冷却
_PROVIDER_BROKEN = re.compile(r"model is unavailable|model_unavailable|upstream request failed|server_error", re.IGNORECASE)

def _retry_wait(result: TaskResult, attempt: int, retry_delay: float, backoff: float) -> float:
    """Exponential backoff for a retry attempt; provider/network failures wait
    at least 30s so a rate-limit window can clear. F line: equal jitter
    breaks the thundering herd — parallel workers that failed together must
    not re-sync into the next retry window together (rate-limit failures
    spread within 30..60s, keeping the >=30s floor; other failures get
    +/-50% around the base)."""
    wait = retry_delay * (backoff ** (attempt - 1))
    reason = result.reason or ""
    if _PROVIDER_BROKEN.search(reason):
        # provider 模型不可用：固定 60s+ 冷却（等上游恢复），连续失败由
        # circuit/stall 治理接管；jitter 防并发惊群。
        wait = max(wait, 60.0) + random.uniform(0, 30.0)
        if attempt >= 3:
            log.warning("Provider model unavailable (attempt %d); consider "
                        "--round-delay after retries exhaust", attempt)
    elif _RETRYABLE_REASON.search(reason):
        wait = max(wait, 30.0)
        wait += random.uniform(0, min(wait, 30.0))
    else:
        wait *= random.uniform(0.5, 1.5)
    return wait

def expand_command_placeholders(cmd: str, cwd: str, output: str = "") -> str:
    """Compatibility adapter for the centralized command expander."""
    return expand_cmd(cmd, output, cwd, config.TOOL_ROOT)

def run_validation(cmd: str, cwd: str, timeout: float = 600,
                   cap: Optional[int] = None, cancel_event=None) -> ValidationResult:
    """Run one validator command in its own process group with a hard
    deadline and bounded diagnostics. A spawn crash fails closed."""
    if not isinstance(cmd, str):
        raise TypeError("validator command must be a string")
    validate_timeout(timeout, "validator")
    output_cap = config.VALIDATION_OUTPUT_MAX_BYTES if cap is None else max(1, cap)
    embedded = embedded_checker_argv(cmd)
    command, shell = (embedded, False) if embedded is not None else (cmd, True)
    if cancel_event is None:
        return run_bounded(command, cwd, timeout, output_cap, shell, "validator")
    return run_bounded(command, cwd, timeout, output_cap, shell, "validator",
                       cancel_event=cancel_event)

def run_argv(cmd: list[str], cwd: str, timeout: float = 30,
             cap: Optional[int] = None) -> ValidationResult:
    """Run an argv command with the same bounded process-tree contract."""
    if (not isinstance(cmd, (list, tuple)) or not cmd
            or not all(isinstance(value, str) for value in cmd)):
        raise TypeError("argv command must be a non-empty string list")
    validate_timeout(timeout, "process")
    output_cap = config.COMMAND_OUTPUT_MAX_BYTES if cap is None else max(1, cap)
    return run_bounded(list(cmd), cwd, timeout, output_cap, False, "process")

REJECTED_EXCERPT_MAX_BYTES = 65536  # bounded diagnostics, never the deliverable

def _save_rejected_excerpt(tmp: Path, workdir: str, cmd: str, reason: str) -> Optional[str]:
    """Keep a bounded excerpt of a validator-rejected artifact under the
    private runtime ``rejected/`` directory so humans and retry prompts can diagnose WHY the
    gate refused it (the deliverable path itself stays clean — zero
    residue at the output location, aero-id maintenance R5 finding).
    Returns the excerpt path ("" when the artifact was already gone)."""
    try:
        text = tmp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    excerpt = text[:REJECTED_EXCERPT_MAX_BYTES]
    if len(text) > REJECTED_EXCERPT_MAX_BYTES:
        excerpt += "\n... (excerpt truncated at %d bytes)" % REJECTED_EXCERPT_MAX_BYTES
    from .config import data_dir
    rej_dir = data_dir(workdir) / "rejected"
    try:
        rej_dir.mkdir(parents=True, exist_ok=True)
        name = tmp.name.removesuffix(".tmp") + "-" + time.strftime("%Y%m%d-%H%M%S") + ".rejected.md"
        out = rej_dir / name
        header = (
            "# Rejected artifact excerpt (diagnostics only; NOT the deliverable)\n\n"
            "- validator: %s\n- reason: %s\n- original output: %s\n\n---\n\n"
            % (cmd, reason, tmp.name)
        )
        out.write_text(header + excerpt, encoding="utf-8")
        return str(out)
    except OSError as exc:
        log.warning("could not keep rejected excerpt: %s", exc)
        return ""

def _apply_gate_commands(tmp: Path, task: Task, commands: list,
                         result: TaskResult, cancel_event=None) -> bool:
    """Run file gate commands concurrently and fold them with AND semantics;
    a failure deletes the artifact, records the outcome on the result
    (T2 feedback fields) and returns False. T12a: a validator may reply
    with a JSON status line -- {"status":"warn"} is allowed with a
    warning, {"status":"fail"} rejects regardless of exit code. H line:
    judge validators (spec.judge) must emit VERDICT: PASS|FAIL - <reason>
    (fail closed on missing/bare verdict). Repo-scoped validators are
    deferred to the stage/batch end (N2) and skipped here."""
    workdir = task.workdir()
    run_one = (run_validation if cancel_event is None else
               lambda command, cwd: run_validation(
                   command, cwd, cancel_event=cancel_event))
    validations = run_file_validators(
        commands, workdir, tmp, expand_command_placeholders,
        run_one, log, "VALIDATE",
    )
    for spec, cmd, v in validations:
        result.validation_ok = v.ok
        result.validation_exit = v.exit_code
        result.validation_stderr = _cap_feedback(v.stderr or "")
        if not _gate_spec_passed(spec, cmd, tmp, result, v, workdir):
            return False
    result.validation_ok = True
    result.validation_exit = 0
    return True

def _reject_artifact(tmp: Path, workdir: str, cmd: str, reason: str,
                     stdout: str = "", stderr: str = "") -> None:
    """Fail-closed disposal of a gate-rejected artifact: keep a bounded
    excerpt under the private runtime ``rejected/`` directory for diagnosis, delete the temp file
    (zero residue at the output path), log the validator output tails."""
    rej = _save_rejected_excerpt(tmp, workdir, cmd, reason)
    if rej:
        log.warning("REJECTED EXCERPT: %s", rej)
    tmp.unlink(missing_ok=True)
    log.warning("VALIDATION FAILED (%s): %s; output NOT saved", reason, cmd)
    for line in (stdout or "").strip().splitlines()[-10:]:
        log.warning("  | %s", line)
    for line in (stderr or "").strip().splitlines()[-10:]:
        log.warning("  | %s", line)

def _gate_spec_passed(spec, cmd: str, tmp: Path, result: TaskResult, v, workdir: str = "") -> bool:
    """Decide whether one gate command passed: exit 0, optional JSON status
    (T12a) and judge VERDICT protocol (fail closed on missing/bare verdict).
    Records failure feedback on the result, deletes the temp artifact and
    returns False on rejection. Rejected artifacts keep a bounded excerpt
    under the private runtime ``rejected/`` directory (never at the output path)."""
    if not v.ok:
        _reject_artifact(tmp, workdir, cmd,
                         "exit=%d%s" % (v.exit_code, " (timeout)" if v.timed_out else ""),
                         v.stdout, v.stderr)
        return False
    status = _json_status(v.stdout)
    if status == "fail":
        _reject_artifact(tmp, workdir, cmd, "JSON status=fail", v.stdout, v.stderr)
        return False
    if spec.judge:
        verdict = judge_verdict(v.stdout)
        if verdict in ("FAIL", "REJECT"):
            line = next((ln.strip() for ln in (v.stdout or "").splitlines()
                         if ln.strip().upper().startswith("VERDICT:")), "")
            result.validation_stderr = _cap_feedback(f"judge {verdict}: {line}")
            _reject_artifact(tmp, workdir, cmd, "judge verdict=%s" % verdict, v.stdout, v.stderr)
            return False
        if verdict is None:
            result.validation_stderr = _cap_feedback(
                "judge produced no VERDICT: PASS|FAIL - <reason> line")
            _reject_artifact(tmp, workdir, cmd, "judge: no verdict", v.stdout, v.stderr)
            return False
        log.info("VALIDATE JUDGE PASS: %s", cmd)
    if status == "warn":
        log.warning("VALIDATE WARN: %s (output saved with warning)", cmd)
    return True

def _save_validated(task: Task, result: TaskResult, validate_cmd: str,
                    cancel_event=None) -> bool:
    """Save a successful result through the engineering gates. The output is
    written to a temp file, every resolved validator command must exit 0
    (AND semantics), then the file is atomically renamed into place; a
    failing gate deletes the temp file and leaves no artifact. {output}
    points at the temp file so gates can inspect the generated content.
    validate_cmd is a comma-separated list of registry names or raw shell
    commands (see _resolve_validators). Returns True when saved."""
    if not result.success or _cancel_requested(cancel_event):
        return False
    commands = _resolve_validator_specs(validate_cmd)
    if not commands:
        save_result(task, result)
        return True
    out_path = task.output_path()
    if out_path is None:
        # no file target: nothing to validate against, print as usual
        save_result(task, result)
        return True
    # T0.1 (round-4 finding H1): the symlink probe must guard the validator
    # path too — _save_validated is used whenever any validator is
    # configured (the recommended config), and without the probe a planted
    # symlink redirects tmp.rename() to its target (arbitrary file
    # overwrite with agent-controlled bytes).
    if _output_path_is_symlink(task):
        log.error("NOT SAVED %s: output path is a symlink (refusing to follow)", out_path)
        return False
    # Local fix (self-iteration round 2, F14): unique temp name (no fixed
    # .tmp suffix a local observer could pre-create as a symlink).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=out_path.name + ".", suffix=".tmp", dir=str(out_path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(result.stdout, encoding="utf-8")
        # Placeholder expansion shell-quotes the temporary path and cwd.
        if not _apply_gate_commands(tmp, task, commands, result, cancel_event):
            return False
        if _cancel_requested(cancel_event):
            return False
        tmp.rename(out_path)
        log.info("WROTE %s (validated)", out_path)
        return True
    finally:
        tmp.unlink(missing_ok=True)

def _json_status(stdout: str) -> str:
    """T12a: extract a validator's JSON status ({"status": "pass|warn|fail"});
    empty when the output carries no JSON status line. When a validator emits
    multiple status objects, the most restrictive status wins."""
    strongest = ""
    rank = {"": 0, "pass": 1, "warn": 2, "fail": 3}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        status = str(data.get("status", "")).lower()
        if status in rank and rank[status] > rank[strongest]:
            strongest = status
    return strongest

_JUDGE_VERDICT_RE = re.compile(r"^\s*\*{0,2}VERDICT\s*:\s*\*{0,2}(PASS|FAIL|REJECT)\b",
                               re.IGNORECASE | re.MULTILINE)
def judge_verdict(text: str) -> Optional[str]:
    """H line (LLM-as-judge protocol): extract a VERDICT: PASS|FAIL|REJECT
    line from a judge validator's stdout. A verdict must carry a reason on
    the same line (bare verdicts fail closed — T12b semantics); any FAIL/
    REJECT wins over an earlier PASS. None = no usable verdict."""
    first = None
    for m in _JUDGE_VERDICT_RE.finditer(text or ""):
        line_end = text.find("\n", m.end())
        if line_end == -1:
            line_end = len(text)
        rest = text[m.end():line_end].strip()
        if not rest:
            continue  # bare verdict without a reason: not a usable judgment
        verdict = m.group(1).upper()
        if verdict in ("FAIL", "REJECT"):
            return verdict
        if first is None:
            first = verdict
    return first

def _cap_feedback(text: str, max_chars: int = 4000, max_lines: int = 40) -> str:
    """Cap validator stderr for retry feedback (decision D4): 4000 chars /
    40 lines, keeping the head of the output (where CLI banners land)."""
    lines = text.splitlines()[:max_lines]
    capped = "\n".join(lines)[:max_chars]
    if len(text) > len(capped):
        capped += "\n... (truncated)"
    return capped

def _retry_task_with_feedback(task: Task, result: TaskResult) -> Task:
    """T2: build the retry task with the previous validator failure appended
    to the prompt so the agent can fix the gate problem directly (the
    feedback is capped per decision D4)."""
    if not result.validation_stderr:
        return task
    feedback = (
        f"\n\n[validator feedback from previous attempt]\n"
        f"exit={result.validation_exit}\n"
        f"{result.validation_stderr}"
    )
    return replace(task, prompt=task.prompt + feedback)

def revalidate_existing(path: Path, validate_cmd: Optional[str], workdir: str = "") -> bool:
    """Re-run the effective validators against an already-written artifact
    (the reuse path: a previously generated output is promoted only while it
    still passes every gate). Missing or empty artifacts fail closed; no
    validators apply -> trivially reusable. Returns True when the artifact
    passes all gates."""
    if not path.is_file():
        return False
    # T0.1 (full-SDLC pipeline): a symlink artifact must never be validated
    # through (the gate could execute against the victim) — fail closed.
    if path.is_symlink():
        log.warning("REVALIDATE: %s is a symlink; failing closed", path)
        return False
    try:
        empty = path.stat().st_size == 0
    except OSError:
        return False  # vanished between is_file and stat: fail closed
    if empty:
        return False
    commands = _resolve_validator_specs(validate_cmd)
    if not commands:
        return True
    wd = workdir or os.getcwd()
    validations = run_file_validators(
        commands, wd, path, expand_command_placeholders,
        run_validation, log, "REVALIDATE",
    )
    for spec, cmd, validation in validations:
        if not _revalidation_passed(spec, path, cmd, validation):
            return False
    log.info("REVALIDATED %s", path)
    return True

def _revalidate_one(spec, path: Path, wd: str) -> bool:
    """Re-run a single validator spec against an existing artifact. T12a:
    the JSON status protocol applies on the reuse path too — an exit-0
    validator reporting {"status":"fail"} must not promote the artifact
    when the fresh-save path would reject it (round-4 finding M3/P2-4).
    H line: judge validators apply the same fail-closed verdict protocol."""
    cmd = expand_cmd(spec.cmd, path, wd, config.TOOL_ROOT)
    log.info("REVALIDATE: %s", cmd)
    v = run_validation(cmd, wd)
    return _revalidation_passed(spec, path, cmd, v)


def _revalidation_passed(spec, path: Path, cmd: str, v) -> bool:
    if not v.ok:
        log.warning("REVALIDATION FAILED%s: %s; %s NOT reusable",
                    " (timeout)" if v.timed_out else f" (exit={v.exit_code})", cmd, path)
        for line in (v.stdout or "").strip().splitlines()[-10:]:
            log.warning("  | %s", line)
        for line in (v.stderr or "").strip().splitlines()[-10:]:
            log.warning("  | %s", line)
        return False
    status = _json_status(v.stdout)
    if status == "fail":
        log.warning("REVALIDATION FAILED (JSON status=fail): %s; %s NOT reusable", cmd, path)
        return False
    if status == "warn":
        log.warning("REVALIDATE WARN: %s (artifact reusable with warning)", cmd)
    if spec.judge:
        verdict = judge_verdict(v.stdout)
        if verdict in ("FAIL", "REJECT"):
            log.warning("REVALIDATION FAILED (judge verdict=%s): %s; %s NOT reusable",
                        verdict, cmd, path)
            return False
        if verdict is None:
            log.warning("REVALIDATION FAILED (judge: no verdict): %s; %s NOT reusable",
                        cmd, path)
            return False
    return True

def _agent_output_reasons(task: Task, stdout_text: str, stderr_text: str,
                          provider_stdout: Optional[str] = None) -> tuple[str, str]:
    agent = task.effective_agent()
    stderr_reason = (agent_failure_reason(0, stderr_text, agent)
                     if stderr_text.strip() else "")
    stdout_reason = (_stdout_banner_reason(stdout_text, agent)
                     if stdout_text.strip() and provider_stdout is None else "")
    provider_stderr = (stderr_text if provider_stdout is None else
                       f"{stderr_text}\n{provider_stdout}")
    provider_reason = _pb_reason(
        provider_stderr, stdout_text if provider_stdout is None else "", agent)
    return stderr_reason or stdout_reason, provider_reason


def _record_provider_health(task: Task, success: bool,
                            provider_reason: str) -> None:
    identity = _pb_identity(task)
    if provider_reason:
        _pb_register_fail(provider_reason, provider=identity)
    elif success:
        _pb_register_ok(provider=identity)


def _result_from_proc(task: Task, proc: subprocess.Popen, stdout_lines: list, stderr_lines: list, elapsed: float) -> TaskResult:
    """Build a result; decode structured transport before signature scans."""
    stderr_text = "".join(stderr_lines)
    stdout_text, transcript_reason, provider_stdout = _projected_agent_stdout(
        task, stdout_lines, proc.returncode)
    output_reason, provider_reason = _agent_output_reasons(
        task, stdout_text, stderr_text, provider_stdout)
    # Exit code first, then body presence, then signatures: operational
    # signatures apply to stderr; the stdout body only rejects line-anchored
    # banners + short error replies (see runner_results docstring).
    reason = _agent_result_reason(
        proc.returncode, stdout_text, transcript_reason, output_reason)
    success = proc.returncode == 0 and not reason
    # Preserve raw structured diagnostics on failure; only success is content.
    result_stdout = ("".join(stdout_lines)
                     if provider_stdout is not None and not success
                     else stdout_text)
    if reason:
        # P7（使用反馈）：拒绝行内联 stderr 首行（provider 400/model
        # unavailable 一眼可见，不必翻到下一行的 FAIL 明细）。
        first_err = (stderr_text or "").strip().splitlines()
        hint = f" | {first_err[0][:200]}" if first_err and first_err[0] else ""
        log.warning("agent output REJECTED: %s%s", reason, hint)
    _record_provider_health(task, success, provider_reason)
    if success:
        log.info("OK  done  [%.1fs]  [output=%s]", elapsed, task.output or "(stdout)")
    else:
        # F2 (retro finding): failures must carry a stderr tail so root
        # causes are visible in run logs without reopening agent sessions.
        tail = "\n".join((stderr_text or "").strip().splitlines()[-3:])[:400]
        detail = f"\n  stderr: {tail}" if tail else ""
        log.warning("FAIL  [code=%d]  [%.1fs]%s", proc.returncode, elapsed, detail)
    return TaskResult(
        task=task, success=success, stdout=result_stdout, stderr=stderr_text,
        elapsed=elapsed, returncode=proc.returncode, reason=reason or "",
    )

def _timeout_result(task: Task, proc: subprocess.Popen, start: float) -> TaskResult:
    """Kill the whole process group (the direct child may have spawned
    helpers that keep pipes open) and return a timed-out result."""
    _kill_group(proc)
    elapsed = time.monotonic() - start
    log.error("TIMEOUT  [%.1fs]  [limit=%ss]", elapsed, task.timeout)
    return TaskResult(
        task=task, success=False, stderr=f"Task timed out after {task.timeout}s",
        elapsed=elapsed, returncode=-1, reason="task timed out",
    )

def _stream_output_enabled() -> bool:
    """Whether agent response bodies should be displayed while running."""
    mode = str(config.STREAM_OUTPUT).lower()
    return mode == "full" or (mode == "auto" and sys.stdout.isatty())

def _stream_proc(proc: subprocess.Popen, prefix: str, start: float,
                 timeout: int, emit: bool = False, stdout_target=None,
                 stderr_target=None, stderr_emit=None,
                 cancel_event=None) -> tuple[list[str], list[str], list]:
    """Drain both pipes to one deadline; reject either stream's cap overflow."""
    stdout_lines, stderr_lines, overflow, stderr_overflow = _drain_process(
        proc, prefix, start, timeout, config.OUTPUT_MAX_BYTES, emit,
        stdout_target, stderr_target, stderr_emit, cancel_event)
    if error := getattr(overflow, "error", "") or getattr(stderr_overflow, "error", ""):
        raise ValueError(error)
    return stdout_lines, stderr_lines, [overflow[0] or stderr_overflow[0]]
def _drain_process(proc: subprocess.Popen, prefix: str, start: float,
                   timeout: float, cap: int, emit: bool = False,
                   stdout_target=None, stderr_target=None,
                   stderr_emit=None, cancel_event=None) -> tuple:
    return _drain_process_impl(
        proc, prefix, start, timeout, cap, emit, stdout_target,
        stderr_target, stderr_emit, cancel_event, _read_stream, _kill_group,
        _PROCESS_POLL_WAIT, _DrainTimeout, _CancelledDrain,
        _join_stream_threads)


def _wait_for_cancel(seconds: float, cancel_event=None) -> bool:
    return _wait_for_cancel_impl(seconds, cancel_event, _scheduled_sleep)


def _run_with_gate(task: Task, validate_cmd: str, index: int, total: int,
                    session_flags: Optional[list] = None, parallel: bool = False,
                    session_name: str = "", attempt: int = 1,
                    trace_task_id: str = "", cancel_event=None) -> TaskResult:
    """Run one task and push its result through the engineering gate;
    a gate rejection marks the task failed with the exit signature."""
    worktree_before = capture_worktree_state(task.workdir())
    trace_task_id = trace_task_id or make_task_id(
        metering.RUN_ID, task.prompt, task.output, task.workdir(), index)
    result = run_task(task, task_index=index, total=total, parallel=parallel,
                      session_flags=session_flags, session_name=session_name,
                      attempt=attempt, trace_task_id=trace_task_id,
                      cancel_event=cancel_event)
    if result.success and _cancel_requested(cancel_event):
        result = _cancelled_result(task)
    if result.success:
        effective_validate = (task.validate if task.validate is not None
                              else validate_cmd)
        result.success = (_save_validated(task, result, effective_validate)
                          if cancel_event is None else
                          _save_validated(task, result, effective_validate,
                                          cancel_event))
        if not result.success:
            result.reason = ("cancelled" if _cancel_requested(cancel_event)
                             else f"validation failed (exit={result.validation_exit})"
                             if result.validation_exit else "validation failed")
    annotate_result(result, trace_task_id,
                    str(task.memory.get("stage", "")), attempt)
    record_validation_event(result, task)
    if not result.success:
        evidence = preserve_failed_worktree(
            task.workdir(), task.output or f"task-{index}", worktree_before)
        if evidence:
            result.reason = f"{result.reason}; worktree evidence: {evidence}".strip("; ")
    record_task(result)
    return result

def _run_retried(task: Task, validate_cmd: str, index: int, total: int,
                 retries: int, retry_delay: float, backoff: float,
                 session_flags=None, retry_flags=None, parallel: bool = False,
                 session_name: str = "", continue_flags=None,
                 cancel_event=None) -> TaskResult:
    """Run once plus exactly retries bounded retry attempts."""
    trace_task_id = make_task_id(
        metering.RUN_ID, task.prompt, task.output, task.workdir(), index)
    result = _run_with_gate(task, validate_cmd, index, total, session_flags,
                            parallel, session_name, attempt=1,
                            trace_task_id=trace_task_id,
                            cancel_event=cancel_event)
    established = result.session_established
    for attempt in range(1, retries + 1):
        if result.success or _cancel_requested(cancel_event):
            break
        if not is_retryable(result.reason):
            log.warning("RETRY SKIPPED for task [%d/%d]: permanent failure (%s)",
                        index, total, result.reason or "no reason")
            break
        wait = _retry_wait(result, attempt, retry_delay, backoff)
        log.warning("RETRY %d/%d for task [%d/%d] in %.0fs (reason: %s)",
                    attempt, retries, index, total, wait,
                    result.reason or f"exit {result.returncode}")
        if not _wait_for_cancel(wait, cancel_event):
            return _cancelled_result(task)
        retry_task = (_retry_task_with_feedback(task, result)
                      if result.validation_stderr else task)
        result = _run_with_gate(retry_task, validate_cmd, index, total,
                                continue_flags if established else retry_flags,
                                parallel, session_name,
                                attempt=attempt + 1,
                                trace_task_id=trace_task_id,
                                cancel_event=cancel_event)
        established = established or result.session_established
        result.session_established = established
    return result


def run_serial(tasks: list[Task], retries: int = 0, retry_delay: float = 10.0,
               backoff: float = 2.0, min_interval: float = 0.0,
               session_mode: str = "new", session_id: str = "",
               session_name: str = "",
               validate_cmd: str = "",
               session_state: Optional[set] = None,
               cancel_event=None, result_callback=None) -> list[TaskResult]:
    """Run serially with validation, retries, throttling, and stable sessions."""
    results = []
    total = len(tasks)
    active_sessions = session_state if session_state is not None else set()
    for i, task in enumerate(tasks, 1):
        if _cancel_requested(cancel_event):
            results.extend(_cancelled_result(item) for item in tasks[i - 1:])
            break
        log.info("-- [%d/%d] --", i, total)
        flags = None
        agent = task.effective_agent()
        session_key = (session_id, os.path.realpath(task.workdir()),
                       agent, task.provider)
        active = session_key in active_sessions
        continue_flags = None
        if session_mode != "new":
            continue_flags = config.agent_session_flags(
                "continue", session_id, session_name, agent)
            if not active:
                flags = config.agent_session_flags(
                    "start", session_id, session_name, agent)
            else:
                flags = _rotation_flags(
                    session_id, session_name, task.workdir(), agent)
        retry_flags = continue_flags if active else flags
        result = _run_retried(task, validate_cmd, i, total, retries,
                              retry_delay, backoff, flags, retry_flags,
                              session_name=session_name,
                              continue_flags=continue_flags,
                              cancel_event=_task_cancel_event(
                                  cancel_event, task))
        results.append(result)
        if result_callback is not None:
            result_callback(result)
        if ((result.success or result.session_established)
                and session_mode != "new"):
            active_sessions.add(session_key)
        if _cancel_requested(cancel_event):
            continue
        if result.success and min_interval > 0:
            _wait_for_cancel(min_interval, cancel_event)
    return results


def run_parallel(tasks: list[Task], workers: int = 0, validate_cmd: str = "",
                 retries: int = 0, retry_delay: float = 10.0,
                 backoff: float = 2.0, min_interval: float = 0.0,
                 waves: Optional[list] = None, cancel_event=None,
                 result_callback=None) -> list[TaskResult]:
    """Run parallel tasks through the bounded scheduling helper."""
    return _run_parallel_impl(
        tasks, workers, validate_cmd, retries, retry_delay, backoff,
        min_interval, waves, cancel_event, _run_retried,
        _wait_for_cancel, kill_active_procs, _cancelled_result,
        _BUDGET_MESSAGE, result_callback)
def print_summary(results: list[TaskResult]) -> None:
    """Print an execution summary table."""
    total = len(results)
    succeeded = sum(1 for r in results if r.success)
    failed = total - succeeded
    total_elapsed = sum(r.elapsed for r in results)
    wall_time = max((r.elapsed for r in results), default=0)

    print("=" * 56)
    print("  pi-batch execution report")
    print("=" * 56)
    print("  total:     %d" % total)
    print("  succeeded: %d" % succeeded)
    print("  failed:    %d" % failed)
    print("  CPU time:  %.1fs" % total_elapsed)
    print("  wall time: %.1fs" % wall_time)
    if total:
        rate = 100.0 * succeeded / total
        print("  成功率:    %.0f%%" % rate)
    if failed:
        print("  下一步:    重试用 --retries；被拒产物在用户级 runtime/rejected/ 可诊断")
    elif succeeded:
        print("  全部通过 ✓ — 可 --git-commit 落库或继续下一轮")
    print()
    for r in results:
        icon = "PASS" if r.success else "FAIL"
        brief = r.task.prompt[:60].replace("\n", " ")
        sig = f"  [val exit={r.validation_exit}]" if (not r.success and r.validation_exit) else ""
        print("  %s  [%6.1fs]%s %s..." % (icon, r.elapsed, sig, brief))
    print("=" * 56)
