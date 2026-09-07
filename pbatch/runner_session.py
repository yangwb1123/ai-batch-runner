"""Task-agent-aware argv and provider-session preparation for the runner."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from typing import Optional

from . import config
from .memory import (enrich_prompt, memory_enabled, new_session_id,
                     session_id_from_flags)
from .models import Task, TaskResult
from .session import (capture_codex_thread, fork_flags, has_compaction,
                      resolve_provider_session, session_file,
                      session_size_bytes)


def _memory_invocation(task: Task, session_flags: Optional[list],
                       session_name: str) -> tuple[list, str, str, str]:
    """Build argv after binding memory/session policy to ``task.agent``."""
    agent = task.effective_agent()
    workdir = task.workdir()
    flags = session_flags
    name = session_name or getattr(flags, "session_name", "") or task.memory.get(
        "stage", "") or "batch"
    kind = getattr(flags, "kind", "")
    session_id = getattr(flags, "session_id", "") or session_id_from_flags(flags)
    prompt = task.prompt
    if kind and session_id:
        # Annotated flags carry logical context, not an adapter binding.
        flags = config.agent_session_flags(kind, session_id, name, agent)
    if memory_enabled(agent):
        prompt = enrich_prompt(
            prompt, workdir, str(task.memory.get("memory_mode", "")))
        if flags is None:
            session_id = new_session_id(name, task.prompt, workdir)
            flags = config.agent_session_flags(
                "start", session_id, name, agent)
            kind = "start"
    provider_bound = (config.agent_session_argv_layout(agent) == "led"
                      and config.agent_capture_thread_id(agent))
    if kind == "start" and provider_bound:
        provider_session, found = resolve_provider_session(
            workdir, name, session_id, "continue", agent, task.provider)
        if found:
            kind = "continue"
            flags = config.agent_session_flags(
                kind, session_id, name, agent,
                provider_session=provider_session)
    elif kind in ("continue", "fork") and provider_bound:
        provider_session, found = resolve_provider_session(
            workdir, name, session_id, kind, agent, task.provider)
        if not found:
            raise ValueError(
                f"{agent} {kind} session mapping is missing for {name!r}")
        flags = config.agent_session_flags(
            kind, session_id, name, agent,
            provider_session=provider_session)
    command = replace(task, prompt=prompt).to_cmd(flags)
    return command, workdir, session_id, name


def _capture_codex_thread(stdout_lines: list, name: str, sid: str,
                          workdir: str, bin_name: str = "",
                          provider: str = "") -> bool:
    """Capture a provider thread using the task executor's adapter."""
    return capture_codex_thread(
        stdout_lines, name, sid, workdir, bin_name or config.AGENT_BIN,
        provider)


def _provider_session_ready(task: Task, name: str, sid: str,
                            workdir: str) -> bool:
    """Whether a capture-thread adapter has a scoped native mapping."""
    agent = task.effective_agent()
    if not config.agent_capture_thread_id(agent):
        return True
    return resolve_provider_session(
        workdir, name, sid, "continue", agent, task.provider)[1]


def bind_spawned_session(result: TaskResult, task: Task,
                         session_flags: Optional[list], stdout_lines: list,
                         name: str, sid: str, workdir: str) -> TaskResult:
    """Mark a spawned invocation resumable, capturing native ids first."""
    if session_flags is None:
        return result
    agent = task.effective_agent()
    if config.agent_capture_thread_id(agent):
        _capture_codex_thread(
            stdout_lines, name, sid, workdir, agent, task.provider)
        result.session_established = _provider_session_ready(
            task, name, sid, workdir)
        if result.success and not result.session_established:
            result.success = False
            result.reason = "provider session mapping was not captured"
    else:
        # Popen succeeded: agent and validator failures still belong to this
        # logical context, so the next attempt must resume rather than start.
        result.session_established = True
    return result


def unexpected_result(exc: Exception, proc, start: float, task: Task,
                      flags: Optional[list], stdout_lines: list, name: str,
                      sid: str, workdir: str) -> TaskResult:
    """Build a bounded unexpected-failure result and retain spawn state."""
    elapsed = time.monotonic() - start
    config.log.error("ERROR  [%.1fs]  [%s]", elapsed, exc)
    result = TaskResult(task=task, success=False, stderr=str(exc),
                        elapsed=elapsed, reason=str(exc))
    if proc is None:
        return result
    return bind_spawned_session(
        result, task, flags, stdout_lines, name, sid, workdir)


def rotation_flags(session_id: str, session_name: str, workdir: str = "",
                   bin_name: str = "") -> list:
    """Continue a session, or fork it past the configured size watermark."""
    agent = bin_name or config.AGENT_BIN
    flags = config.agent_session_flags(
        "continue", session_id, session_name, agent)
    cwd = workdir or os.getcwd()
    if has_compaction(cwd, session_id):
        config.log.warning(
            "SESSION: compaction entry in %s (context was shrunk by pi)",
            session_id)
    active_file = session_file(cwd, session_id)
    if (active_file and session_size_bytes(cwd, session_id)
            > config.SESSION_MAX_BYTES):
        config.log.warning("SESSION ROTATION: %s exceeds %d bytes; forking",
                           active_file, config.SESSION_MAX_BYTES)
        return fork_flags(str(active_file), agent)
    return flags
