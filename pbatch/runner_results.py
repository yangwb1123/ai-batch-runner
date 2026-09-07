"""Small failure-result constructors shared by the task runner."""

from __future__ import annotations

import json
import re
import time
from typing import Optional

from . import config
from .config import log
from .models import Task, TaskResult
from .session import valid_codex_thread_id


_STDOUT_BANNER_RE = re.compile(r"^\[?error\]?:|^fatal:", re.IGNORECASE | re.MULTILINE)
_SHORT_BODY_LINES = 8  # a body this small that contains a signature is
# a provider error reported AS the answer, not a doc quote.
_CODEX_TERMINALS = frozenset({"turn.completed", "turn.failed", "error"})
_CODEX_FAILURE_FIELDS = ("error", "message", "reason", "detail", "code")
_NO_MESSAGE = object()


def expects_codex_transcript(task: Task) -> bool:
    """Whether this adapter explicitly requests Codex JSONL on stdout."""
    return config.agent_output_protocol(task.effective_agent()) == "codex-jsonl"


def _logical_stdout_lines(stdout_lines: list) -> list[str]:
    """Reassemble JSONL records split by the bounded pipe reader.

    ``_read_stream`` intentionally reads in 8 KiB chunks, so one large Codex
    command event can occupy several collector entries even though it is one
    JSONL record.  The complete stdout is already bounded by
    ``OUTPUT_MAX_BYTES`` at this point.
    """
    # JSONL is delimited only by LF (with an optional CR from CRLF). Python's
    # str.splitlines() also treats U+2028/U+2029 as separators and would
    # corrupt valid assistant text embedded in a JSON string.
    return [line[:-1] if line.endswith("\r") else line
            for line in "".join(str(part) for part in stdout_lines).split("\n")]


def projected_agent_stdout(task: Task, stdout_lines: list,
                           returncode: int) -> tuple[str, str, Optional[str]]:
    """Project stdout and choose what provider-health matching may inspect.

    Plain adapters retain their original body scan (``None``).  For Codex
    JSONL, successful transport exposes neither command events nor assistant
    prose to the circuit breaker.  Failed or malformed transport exposes only
    explicit ``turn.failed``/``error`` payloads; the raw transcript remains a
    diagnostic stream and is never provider-health evidence.
    """
    raw = "".join(stdout_lines)
    if not expects_codex_transcript(task):
        return raw, "", None
    body, transcript_error = codex_transcript_output(stdout_lines)
    health_text = codex_failure_payloads(stdout_lines)
    return body, transcript_error, health_text


def _render_codex_failure(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def codex_failure_payloads(stdout_lines: list) -> str:
    """Extract only provider-authored failure payloads from Codex JSONL.

    This scan is deliberately tolerant because the strict transcript parser
    separately rejects malformed transport.  Invalid lines and all non-error
    events are ignored rather than promoted into provider health evidence.
    """
    payloads: list[str] = []
    for raw in _logical_stdout_lines(stdout_lines):
        try:
            event = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or event.get("type") not in {
                "turn.failed", "error"}:
            continue
        for field in _CODEX_FAILURE_FIELDS:
            if field in event and event[field] is not None:
                payloads.append(_render_codex_failure(event[field]))
    return "\n".join(payloads)


def agent_result_reason(returncode: int, stdout: str,
                        transcript_error: str, output_error: str) -> str:
    """Apply stable result precedence after structured transport parsing."""
    if returncode != 0:
        return f"agent exited {returncode}"
    if transcript_error:
        return transcript_error
    if not stdout.strip():
        return "agent produced no output"
    return output_error


def _decode_codex_event(raw: str, line_number: int) -> tuple[dict, str]:
    """Decode one non-empty Codex JSONL event with bounded diagnostics."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return {}, f"malformed Codex JSONL event at line {line_number}"
    if not isinstance(value, dict):
        return {}, f"Codex JSONL event at line {line_number} is not an object"
    event_type = value.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        return {}, f"Codex JSONL event at line {line_number} has no type"
    return value, ""


def _completed_codex_message(event: dict, line_number: int):
    """Project a completed assistant item; validate every item envelope."""
    event_type = event["type"]
    if not event_type.startswith("item."):
        return _NO_MESSAGE, ""
    item = event.get("item")
    if not isinstance(item, dict):
        return _NO_MESSAGE, (
            f"Codex JSONL {event_type} at line {line_number} has no item object")
    item_type = item.get("type")
    if not isinstance(item_type, str) or not item_type.strip():
        return _NO_MESSAGE, (
            f"Codex JSONL {event_type} at line {line_number} has no item type")
    if event_type != "item.completed" or item_type != "agent_message":
        return _NO_MESSAGE, ""
    text = item.get("text")
    if not isinstance(text, str):
        return _NO_MESSAGE, (
            f"Codex JSONL agent message at line {line_number} is not text")
    return text, ""


def codex_transcript_output(stdout_lines: list) -> tuple[str, str]:
    """Return only the final completed assistant message from Codex JSONL.

    The transport is fail-closed: every non-empty line must be a typed JSON
    object, a successful terminal must be last, and it must follow a
    substantive completed ``agent_message``.  The caller retains the raw
    list separately for native thread-id capture.
    """
    messages: list[str] = []
    terminal = ""
    saw_event = False
    for line_number, raw in enumerate(_logical_stdout_lines(stdout_lines), 1):
        if not str(raw).strip():
            continue
        event, error = _decode_codex_event(raw, line_number)
        if error:
            return "", error
        if not saw_event:
            thread_id = event.get("thread_id")
            if (event["type"] != "thread.started"
                    or not isinstance(thread_id, str)
                    or not valid_codex_thread_id(thread_id)):
                return "", (
                    "Codex JSONL transcript must start with a valid "
                    "thread.started event")
        saw_event = True
        if terminal:
            return "", "Codex JSONL transcript has events after terminal event"
        event_type = event["type"]
        if event_type in _CODEX_TERMINALS:
            terminal = event_type
            continue
        message, error = _completed_codex_message(event, line_number)
        if error:
            return "", error
        if message is not _NO_MESSAGE:
            messages.append(message)
    if not saw_event:
        return "", "Codex JSONL transcript has no events"
    if terminal in ("turn.failed", "error"):
        return "", f"Codex JSONL transcript ended with {terminal}"
    if terminal != "turn.completed":
        return "", "Codex JSONL transcript has no turn.completed event"
    if not messages or not messages[-1].strip():
        return "", "Codex JSONL transcript has no final agent message"
    return messages[-1], ""


def agent_stdout_banner_reason(stdout: str, bin_name: str = "",
                               scan_short_body: bool = True) -> str:
    """Reject suspicious plain-text BODIES while sparing normal ones. Provider
    signatures (quota/rate-limit/network) belong to the OPERATIONAL stream
    (stderr); structured transports bypass this helper entirely. Plain text
    is scanned with two narrow rules only:

    - line-anchored ``ERROR:``/``fatal:`` banners are real CLI banners;
    - for plain-text adapters, a SHORT body (<= _SHORT_BODY_LINES lines)
      matching a signature is a provider error reported AS the answer;
    """
    m = _STDOUT_BANNER_RE.search(stdout)
    if m:
        line = stdout.splitlines()[0][:120]
        return f"agent output starts with a CLI banner: {line}"
    lines = [ln.strip() for ln in stdout.strip().splitlines() if ln.strip()]
    if scan_short_body and lines and len(lines) <= _SHORT_BODY_LINES:
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
    """Reject any single generated argv value over the safe prompt budget.

    Session-led adapters place the prompt at different positions for start,
    continue, and fork. Inspecting the actual argv avoids a stale fixed index
    and also prevents another generated positional value from causing E2BIG.
    """
    sizes = [len(str(value).encode("utf-8", errors="replace"))
             for value in cmd[1:]]
    size = max(sizes, default=0)
    if config.PROMPT_MAX_BYTES <= 0 or size <= config.PROMPT_MAX_BYTES:
        return None
    reason = f"prompt exceeds {config.PROMPT_MAX_BYTES} bytes ({size} bytes)"
    log.error("REJECTED: %s", reason)
    return TaskResult(task=task, success=False, returncode=-1, reason=reason)
