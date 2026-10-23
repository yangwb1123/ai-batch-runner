"""Operator-declared readiness probes for CLI coding agents.

pi-batch never guesses a vendor's authentication state and never asks a model
whether an agent is usable.  Readiness is an operator contract: a bounded local
command whose exit status answers "can this agent start work right now?".

Configuration (project ``pi-batch.yaml`` or user settings):

    agent:
      readiness_cmd: "pi auth check --json"        # all agents
      agents:
        codex:
          readiness_cmd: ["codex", "login", "status"]   # one binary

A string is split with :func:`shlex.split` and executed WITHOUT a shell, so
pipes, redirects and ``$VARS`` are literal argv tokens rather than shell
syntax.  Unconfigured probes, spawn failures, timeouts, oversized output and
malformed configuration are ``unknown`` -- never ``ready``.

The probe is observational: it does not write the ledger, does not touch the
campaign state and never invokes a model.  Only a bounded, sanitized and
best-effort redacted diagnostic tail is exposed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import time
from dataclasses import dataclass
from typing import Optional

from . import config
from .process_exec import run_bounded

READY = "ready"
NOT_READY = "not_ready"
UNKNOWN = "unknown"
STATUSES = (READY, NOT_READY, UNKNOWN)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_OUTPUT_CAP = 8 * 1024
MAX_CONFIG_CHARS = 4096
MAX_TOKENS = 32
MAX_TOKEN_CHARS = 256
MAX_AGENT_CHARS = 128
MAX_SOURCE_CHARS = 160
MAX_COMMAND_CHARS = 240
MAX_REASON_CHARS = 300
MAX_DIAGNOSTIC_CHARS = 400
EVIDENCE_ITEM_CHARS = 240
_LABEL = "agent readiness"
_OVERFLOW_MARKER = f"{_LABEL} output exceeds {DEFAULT_OUTPUT_CAP} bytes"

# Best-effort redaction of credential-shaped words in diagnostic text.  This
# is a bounded display filter, not a credential scanner: the probe output is
# operator-owned data and pi-batch never reads credential files itself.
_SECRET = re.compile(
    r"(?i)(?<![a-z0-9])(token|secret|passwords?|passwd|api[_-]?key|"
    r"private[_-]?key|credentials?|authorization|bearer)(?![a-z0-9])")
_SCHEME = re.compile(r"(?i)^(bearer|basic|digest)$")


def _single_line(text: object) -> str:
    """Collapse whitespace and drop control characters from untrusted text."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    return " ".join(cleaned.split())


def _clip(text: str, limit: int) -> str:
    """Bound a single-line diagnostic without keeping the unbounded prefix."""
    return text if len(text) <= limit else text[:max(0, limit - 3)] + "..."


def _clip_tail(text: str, limit: int) -> str:
    """Keep the informative end of a bounded output, never the whole body."""
    return text if len(text) <= limit else "..." + text[-(limit - 3):]


def redact(text: object) -> str:
    """Best-effort redaction of credential-shaped tokens in display text."""
    out = []
    redact_next = False
    for token in _single_line(text).split():
        if redact_next:
            out.append("<redacted>")
            redact_next = False
            continue
        if _SCHEME.match(token.rstrip(":,")):
            out.append(token)
            redact_next = True
            continue
        label = token.rstrip(":,")
        if label != token and _SECRET.search(label):
            out.append(token)
            redact_next = True
            continue
        key, separator, _value = token.partition("=")
        if _SECRET.search(key if separator else token):
            if separator:
                out.append(key + "=<redacted>")
            elif token.startswith("-"):
                out.append(token)
                redact_next = True
            else:
                out.append("<redacted>")
            continue
        out.append(token)
    return " ".join(out)


def _parse_argv(value: object) -> tuple:
    """Normalize a declared readiness command without shell interpolation."""
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("readiness_cmd string is empty")
        if len(value) > MAX_CONFIG_CHARS:
            raise ValueError(
                f"readiness_cmd string exceeds {MAX_CONFIG_CHARS} characters")
        try:
            tokens = shlex.split(value, comments=False, posix=True)
        except ValueError:
            raise ValueError("readiness_cmd string is not parseable as argv") from None
    elif isinstance(value, (list, tuple)):
        if not value:
            raise ValueError("readiness_cmd argv list is empty")
        tokens = list(value)
    else:
        raise ValueError("readiness_cmd must be a string or an argv list")
    if not all(isinstance(token, str) and token for token in tokens):
        raise ValueError("readiness_cmd argv must contain only non-empty strings")
    if len(tokens) > MAX_TOKENS:
        raise ValueError(f"readiness_cmd argv exceeds {MAX_TOKENS} entries")
    for token in tokens:
        if len(token) > MAX_TOKEN_CHARS:
            raise ValueError(
                f"readiness_cmd argv entry exceeds {MAX_TOKEN_CHARS} characters")
        if "\x00" in token:
            raise ValueError("readiness_cmd argv entry contains a NUL byte")
    if not tokens[0].strip():
        raise ValueError("readiness_cmd executable is empty")
    return tuple(tokens)


def declared_argv(bin_name: str = "") -> tuple:
    """Resolve the declared probe: ``(argv, source, error)``.

    ``argv`` empty with an empty ``error`` means "not configured".  A malformed
    declaration returns an error and must be classified ``unknown``; it never
    falls back to a different agent's command or to "ready".
    """
    name = str(bin_name or config.AGENT_BIN or "").strip()
    section = config._agent_section(name)
    if "readiness_cmd" in section:
        value, source = section["readiness_cmd"], f"agent.agents.{name}.readiness_cmd"
    elif "readiness_cmd" in config._AGENT_CFG:
        value, source = config._AGENT_CFG["readiness_cmd"], "agent.readiness_cmd"
    else:
        return (), "", ""
    try:
        return _parse_argv(value), source, ""
    except ValueError as exc:
        return (), source, str(exc)


@dataclass(frozen=True)
class ReadinessResult:
    """Bounded observation of one operator-declared readiness probe."""

    agent: str
    status: str
    source: str
    argv: tuple
    reason: str
    exit_code: Optional[int]
    timed_out: bool
    duration_ms: Optional[int]
    tail: str

    @property
    def configured(self) -> bool:
        """True only when a valid probe command was resolved."""
        return bool(self.argv)

    def command(self) -> str:
        """Sanitized, redacted, bounded command identity for evidence."""
        if not self.argv:
            return ""
        return _clip(redact(" ".join(self.argv)), MAX_COMMAND_CHARS)

    def evidence(self) -> list:
        """Safe, bounded evidence lines for campaign events and logs."""
        items = [f"agent={self.agent or '<empty>'}", f"readiness={self.status}"]
        if self.source:
            items.append(f"source={self.source}")
        if self.command():
            items.append(f"command={self.command()}")
        if self.exit_code is not None:
            items.append(f"exit_code={self.exit_code}")
        if self.timed_out:
            items.append("timed_out=true")
        if self.tail:
            items.append(f"diagnostic={self.tail}")
        return [_clip(_single_line(item), EVIDENCE_ITEM_CHARS) for item in items]

    def as_dict(self) -> dict:
        """Stable bounded JSON shape; unknown values stay ``None``, never zero."""
        return {
            "agent": _clip(_single_line(self.agent), MAX_AGENT_CHARS),
            "status": self.status,
            "configured": self.configured,
            "source": _clip(_single_line(self.source), MAX_SOURCE_CHARS) or None,
            "command": self.command() or None,
            "exit_code": self.exit_code,
            "timed_out": bool(self.timed_out),
            "duration_ms": self.duration_ms,
            "reason": self.reason,
            "diagnostic": self.tail or None,
        }


def _result(agent: str, status: str, source: str, argv: tuple, reason: str,
            exit_code: Optional[int] = None, timed_out: bool = False,
            duration_ms: Optional[int] = None, tail: str = "") -> ReadinessResult:
    return ReadinessResult(
        agent=_clip(_single_line(agent), MAX_AGENT_CHARS),
        status=status,
        source=_clip(_single_line(source), MAX_SOURCE_CHARS),
        argv=tuple(argv),
        reason=_clip(redact(reason), MAX_REASON_CHARS),
        exit_code=exit_code,
        timed_out=bool(timed_out),
        duration_ms=duration_ms,
        tail=_clip_tail(_single_line(tail), MAX_DIAGNOSTIC_CHARS),
    )


def _diagnostic(outcome) -> str:
    """Keep only a bounded, sanitized tail of stderr/stdout."""
    combined = "\n".join(
        part for part in (outcome.stderr, outcome.stdout) if part and part.strip())
    return _clip_tail(redact(combined), MAX_DIAGNOSTIC_CHARS)


def _classify(agent: str, source: str, argv: tuple, outcome,
              duration_ms: int) -> ReadinessResult:
    """Map a bounded command outcome to exactly ready/not_ready/unknown."""
    tail = _diagnostic(outcome)
    if outcome.timed_out:
        return _result(
            agent, UNKNOWN, source, argv,
            f"readiness probe timed out after {DEFAULT_TIMEOUT_SECONDS:g}s",
            None, True, duration_ms, tail)
    code = outcome.exit_code
    if _OVERFLOW_MARKER in (outcome.stderr or ""):
        return _result(
            agent, UNKNOWN, source, argv,
            f"readiness probe output exceeded {DEFAULT_OUTPUT_CAP} bytes",
            None, False, duration_ms, tail)
    if code is None or code < 0:
        return _result(
            agent, UNKNOWN, source, argv,
            "readiness probe did not complete (spawn failure or signal)",
            None, False, duration_ms, tail)
    if code == 0:
        return _result(agent, READY, source, argv,
                       "readiness probe exited 0", code, False, duration_ms, tail)
    return _result(
        agent, NOT_READY, source, argv,
        f"readiness probe exited with code {code}", code, False, duration_ms, tail)


def probe(bin_name: str = "", *, cwd: str = "") -> ReadinessResult:
    """Run one bounded local probe.  No model, provider or network call."""
    name = str(bin_name or config.AGENT_BIN or "").strip()
    try:
        argv, source, error = declared_argv(name)
    except Exception as exc:  # malformed config must not crash the caller
        argv, source = (), ""
        error = f"readiness configuration unreadable ({type(exc).__name__})"
    if error:
        return _result(name, UNKNOWN, source, (),
                       f"invalid readiness configuration: {error}")
    if not argv:
        return _result(name, UNKNOWN, "", (), "no readiness_cmd configured")
    started = time.monotonic()
    try:
        outcome = run_bounded(list(argv), str(cwd or os.getcwd()),
                              DEFAULT_TIMEOUT_SECONDS, DEFAULT_OUTPUT_CAP,
                              False, _LABEL)
    except (OSError, ValueError) as exc:
        return _result(name, UNKNOWN, source, argv,
                       f"readiness probe could not start ({type(exc).__name__})")
    duration_ms = max(0, int(round((time.monotonic() - started) * 1000)))
    return _classify(name, source, argv, outcome, duration_ms)


def _agent_argument(value: str) -> str:
    """Bound the CLI agent name before any probe can start."""
    name = str(value or "").strip()
    if (not name or len(name) > MAX_AGENT_CHARS or any(ch.isspace() for ch in name)
            or any(ord(ch) < 32 for ch in name)):
        raise argparse.ArgumentTypeError(
            f"agent must be 1-{MAX_AGENT_CHARS} printable characters without spaces")
    return name


def add_readiness_parser(subparsers) -> None:
    """Register ``agents readiness`` on the existing agents subparser."""
    parser = subparsers.add_parser(
        "readiness", help="probe operator-declared agent readiness (read-only)")
    parser.add_argument("--agent", type=_agent_argument, default=None)
    parser.add_argument("--json", action="store_true")


def _default_agents() -> tuple:
    from .agent_ledger import DEFAULT_AGENTS
    return DEFAULT_AGENTS


def _print_readiness(rows: list) -> None:
    print(f"{'AGENT':<16} {'STATUS':<10} {'CONFIGURED':<11} {'EXIT':<5} DIAGNOSTIC")
    for row in rows:
        exit_code = "-" if row["exit_code"] is None else row["exit_code"]
        print(f"{row['agent'][:16]:<16} {row['status']:<10} "
              f"{('yes' if row['configured'] else 'no'):<11} {exit_code:<5} "
              f"{(row['diagnostic'] or '-')[:80]}")


def readiness_main(args) -> int:
    """Entry point for ``pi-batch agents readiness``: read-only observation."""
    name = str(getattr(args, "agent", "") or "").strip()
    names = [name] if name else _default_agents()
    rows = [probe(name, cwd=os.getcwd()).as_dict() for name in names]
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        _print_readiness(rows)
    return 0
