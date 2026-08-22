"""Small failure-result constructors shared by the task runner."""

from __future__ import annotations

import re
import time
from typing import Optional

from . import config
from .config import log
from .models import Task, TaskResult


_STDOUT_BANNER_RE = re.compile(r"^\[?error\]?:|^fatal:", re.IGNORECASE | re.MULTILINE)
_SHORT_BODY_LINES = 8  # a body this small that contains a signature is
# a provider error reported AS the answer, not a doc quote.


def agent_stdout_banner_reason(stdout: str, bin_name: str = "") -> str:
    """Reject suspicious model BODIES while sparing normal ones. Provider
    signatures (quota/rate-limit/network) belong to the OPERATIONAL stream
    (stderr); the body is scanned with two narrow rules only:

    - line-anchored ``ERROR:``/``fatal:`` banners are real CLI banners;
    - a SHORT body (<= _SHORT_BODY_LINES lines) matching a signature is
      a provider error reported AS the answer (offline-agent regressions),
      not a quote from project docs (which is always embedded in longer
      analysis — the codex transcript self-match lesson).
    """
    m = _STDOUT_BANNER_RE.search(stdout)
    if m:
        line = stdout.splitlines()[0][:120]
        return f"agent output starts with a CLI banner: {line}"
    lines = [ln.strip() for ln in stdout.strip().splitlines() if ln.strip()]
    if lines and len(lines) <= _SHORT_BODY_LINES:
        joined = "\n".join(lines)
        for pattern in config.agent_error_patterns(bin_name):
            if pattern.search(joined):
                return f"agent replied with a provider failure ({pattern.pattern})"
    return ""


def overflow_result(task: Task, proc, stdout_lines: list,
                    stderr_lines: list, start: float) -> TaskResult:
    """Reject output whose collected stdout exceeded the configured cap."""
    log.error("REJECTED: agent output exceeds %d bytes (T12e cap)",
              config.OUTPUT_MAX_BYTES)
    return TaskResult(
        task=task, success=False, stdout="".join(stdout_lines),
        stderr="".join(stderr_lines), elapsed=time.monotonic() - start,
        returncode=proc.returncode,
        reason=f"agent output exceeds {config.OUTPUT_MAX_BYTES} bytes (T12e cap)",
    )


def prompt_limit_result(task: Task, cmd: list) -> Optional[TaskResult]:
    """Return a rejection when the adapter prompt exceeds the argv budget."""
    prompt_index = config.agent_prompt_index(config.AGENT_BIN)
    if prompt_index >= len(cmd):
        reason = "agent adapter did not produce a prompt argument"
        return TaskResult(task=task, success=False, returncode=-1, reason=reason)
    size = len(cmd[prompt_index].encode("utf-8", errors="replace"))
    if config.PROMPT_MAX_BYTES <= 0 or size <= config.PROMPT_MAX_BYTES:
        return None
    reason = f"prompt exceeds {config.PROMPT_MAX_BYTES} bytes ({size} bytes)"
    log.error("REJECTED: %s", reason)
    return TaskResult(task=task, success=False, returncode=-1, reason=reason)
