"""Supervision policy for an unattended multi-lane implementation loop.

The loop has four operator-visible properties, each enforced here rather than in
the supervisor process (which is expected to be restarted):

* **The window is anchored once.** ``lane_window_started`` is written to an
  append-only ledger before the first round. A restarted supervisor reuses it, so
  restarting can never extend a 12-hour authorisation or refill a budget.
* **Only liveness failures restart.** A round that exited without a terminal
  receipt (killed, timed out, crashed, machine reboot) is retried with bounded
  exponential backoff. A round that finished and failed a gate, or that reported
  a blocking decision, stops the loop: retrying a deterministic failure only
  hides it and spends money.
* **Every decision is a receipt.** Round outcomes, restarts and the final stop
  reason are appended to the same ledger, so a later reader can reconstruct what
  the supervisor observed instead of trusting a screenshot of a terminal.
* **Corruption stops.** An unreadable or malformed ledger yields ``stop`` with an
  explicit reason; it never silently starts a fresh window.

This module decides *whether* to continue. It does not spawn agents, run gates,
merge, or write outside the private runtime ledger.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1
LEDGER_MAX_BYTES = 4 * 1024 * 1024
LEDGER_MAX_LINE_BYTES = 8 * 1024
MAX_RESTART_ATTEMPTS = 3
RESTART_BACKOFF_SECONDS = 30.0
MAX_WINDOW_HOURS = 24 * 7
MAX_ROUND_INVOCATIONS = 64

COMPLETED = "completed"
CRASHED = "crashed"
GATE_FAILED = "gate_failed"
BLOCKED = "blocked"
STOPPED = "stopped"

RESTART = "restart"
FINISH = "finish"
STOP = "stop"

WINDOW_STARTED = "lane_window_started"
ROUND_FINISHED = "lane_round_finished"
RESTART_SCHEDULED = "lane_restart_scheduled"
LOOP_STOPPED = "lane_loop_stopped"


class LaneLedgerError(ValueError):
    """The supervision ledger cannot be trusted; the loop must not continue."""


@dataclass(frozen=True)
class Window:
    started_at: float
    deadline_at: float
    invocation_budget: int

    def remaining_seconds(self, now: float) -> float:
        return max(0.0, float(self.deadline_at) - float(now))

    def expired(self, now: float) -> bool:
        return self.remaining_seconds(now) <= 0


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    backoff_seconds: float = 0.0
    consumed_invocations: int = 0

    @property
    def continues(self) -> bool:
        return self.action in (RESTART, FINISH)


def ledger_path(root: Path) -> Path:
    from . import config

    return config.data_dir(str(root)) / "lanes" / "supervision.jsonl"


def _validate_window(hours, invocations) -> tuple[float, int]:
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        raise ValueError("window hours must be a number")
    hours = float(hours)
    if not math.isfinite(hours) or hours <= 0 or hours > MAX_WINDOW_HOURS:
        raise ValueError("window hours outside 0..%d" % MAX_WINDOW_HOURS)
    if isinstance(invocations, bool) or not isinstance(invocations, int):
        raise ValueError("invocation budget must be an integer")
    if invocations < 1:
        raise ValueError("invocation budget must be positive")
    return hours, invocations


def _append(path: Path, record: dict) -> None:
    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > LEDGER_MAX_LINE_BYTES:
        raise LaneLedgerError("supervision record exceeds the per-line budget")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".supervision.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        size = path.stat().st_size if path.exists() else 0
        if size + len(encoded) > LEDGER_MAX_BYTES:
            raise LaneLedgerError("supervision ledger exceeds its byte budget")
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _read(path: Path) -> list:
    if not path.is_file() or path.is_symlink():
        return []
    if path.stat().st_size > LEDGER_MAX_BYTES:
        raise LaneLedgerError("supervision ledger exceeds its byte budget")
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="strict").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise LaneLedgerError("supervision ledger line %d is not JSON" % number) from exc
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
            raise LaneLedgerError("supervision ledger line %d has no schema" % number)
        records.append(value)
    return records


def start_window(root: Path, *, hours, invocations, now: Optional[float] = None) -> Window:
    """Return the anchored window, creating it once. Never extends or refills."""
    hours_value, budget = _validate_window(hours, invocations)
    now = time.time() if now is None else float(now)
    path = ledger_path(root)
    records = _read(path)
    for record in records:
        if record.get("kind") != WINDOW_STARTED:
            continue
        window = _window_from(record)
        if window.invocation_budget != budget:
            raise LaneLedgerError(
                "existing window budget %d differs from requested %d; refusing to "
                "reinterpret an authorised window" % (window.invocation_budget, budget))
        return window
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": WINDOW_STARTED,
        "at": now,
        "started_at_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "deadline_at": now + hours_value * 3600.0,
        "invocation_budget": budget,
    }
    _append(path, record)
    return _window_from(record)


def _window_from(record: dict) -> Window:
    try:
        started = float(record["at"])
        deadline = float(record["deadline_at"])
        budget = int(record["invocation_budget"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LaneLedgerError("window record is incomplete") from exc
    if not (math.isfinite(started) and math.isfinite(deadline)) or deadline <= started:
        raise LaneLedgerError("window record has an invalid interval")
    if budget < 1:
        raise LaneLedgerError("window record has an invalid budget")
    return Window(started_at=started, deadline_at=deadline, invocation_budget=budget)


def consumed_invocations(records: list) -> int:
    total = 0
    for record in records:
        if record.get("kind") != ROUND_FINISHED:
            continue
        used = record.get("invocations", 0)
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            raise LaneLedgerError("round record has an invalid invocation count")
        total += used
    return total


def _stopped(records: list) -> Optional[str]:
    for record in reversed(records):
        if record.get("kind") == LOOP_STOPPED:
            return str(record.get("reason", "stopped"))
        if record.get("kind") == WINDOW_STARTED:
            return None
    return None


def classify(*, terminal_receipt: bool, exit_code, gate_ok) -> str:
    """Map one finished round to a supervision class.

    ``terminal_receipt`` means the round wrapper recorded its own end state. A
    missing receipt is a liveness failure: the wrapper never got to report.
    """
    if not terminal_receipt:
        return CRASHED
    if gate_ok is True:
        return COMPLETED if (exit_code == 0 or exit_code is None) else BLOCKED
    if gate_ok is False:
        return GATE_FAILED
    return BLOCKED


def _restart_attempts(records: list) -> int:
    return sum(1 for record in records if record.get("kind") == RESTART_SCHEDULED)


def decide(root: Path, *, classification: str, invocations, now: Optional[float] = None,
           max_restarts: int = MAX_RESTART_ATTEMPTS) -> Decision:
    """Decide the next supervision action and record it. Never raises on policy."""
    now = time.time() if now is None else float(now)
    if classification not in (COMPLETED, CRASHED, GATE_FAILED, BLOCKED):
        raise ValueError("unknown round classification: " + str(classification))
    if isinstance(invocations, bool) or not isinstance(invocations, int) or invocations < 0:
        raise ValueError("round invocations must be a non-negative integer")
    if invocations > MAX_ROUND_INVOCATIONS:
        raise ValueError("round invocations exceed the supervision budget")
    path = ledger_path(root)
    records = _read(path)
    if not records or records[0].get("kind") != WINDOW_STARTED:
        raise LaneLedgerError("no anchored window; start the window before deciding")
    window = _window_from(records[0])
    previous_stop = _stopped(records)
    if previous_stop is not None:
        return Decision(STOP, "loop already stopped: " + previous_stop, consumed_invocations=consumed_invocations(records))
    spent = consumed_invocations(records)

    def stop(reason: str) -> Decision:
        _append(path, {"schema_version": SCHEMA_VERSION, "kind": LOOP_STOPPED,
                       "at": now, "reason": reason})
        return Decision(STOP, reason, consumed_invocations=spent)

    _append(path, {"schema_version": SCHEMA_VERSION, "kind": ROUND_FINISHED, "at": now,
                   "classification": classification, "invocations": invocations})
    spent += invocations
    if classification in (GATE_FAILED, BLOCKED):
        return stop("round %s: retrying a deterministic failure hides it" % classification)
    if classification == CRASHED:
        attempts = _restart_attempts(records)
        if attempts >= max_restarts:
            return stop("restart budget exhausted after %d crash restarts" % attempts)
        if window.expired(now):
            return stop("deadline exhausted during a crashed round")
        if spent >= window.invocation_budget:
            return stop("invocation budget exhausted during a crashed round")
        backoff = min(RESTART_BACKOFF_SECONDS * (2 ** attempts), 600.0)
        _append(path, {"schema_version": SCHEMA_VERSION, "kind": RESTART_SCHEDULED,
                       "at": now, "attempt": attempts + 1, "backoff_seconds": backoff})
        return Decision(RESTART, "round crashed without a terminal receipt",
                        backoff_seconds=backoff, consumed_invocations=spent)
    if window.expired(now):
        return stop("deadline reached")
    if spent >= window.invocation_budget:
        return stop("invocation budget reached")
    return Decision(FINISH, "round completed; the loop may start the next one",
                    consumed_invocations=spent)


def stop(root: Path, reason: str, now: Optional[float] = None) -> None:
    """Record an operator or backlog stop once; later rounds see it and stop."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("stop reason must be a non-empty string")
    now = time.time() if now is None else float(now)
    path = ledger_path(root)
    records = _read(path)
    if not records or records[0].get("kind") != WINDOW_STARTED:
        raise LaneLedgerError("no anchored window; refusing to record a stop")
    if _stopped(records) is not None:
        return
    _append(path, {"schema_version": SCHEMA_VERSION, "kind": LOOP_STOPPED,
                   "at": now, "reason": reason.strip()[:300]})


def status(root: Path, now: Optional[float] = None) -> dict:
    """Read-only summary for a supervisor or an operator dashboard."""
    now = time.time() if now is None else float(now)
    records = _read(ledger_path(root))
    if not records or records[0].get("kind") != WINDOW_STARTED:
        return {"anchored": False, "remaining_seconds": None, "consumed": None,
                "budget": None, "rounds": 0, "restarts": 0, "stopped": None}
    window = _window_from(records[0])
    return {
        "anchored": True,
        "started_at": window.started_at,
        "deadline_at": window.deadline_at,
        "remaining_seconds": round(window.remaining_seconds(now), 3),
        "budget": window.invocation_budget,
        "consumed": consumed_invocations(records),
        "rounds": sum(1 for record in records if record.get("kind") == ROUND_FINISHED),
        "restarts": _restart_attempts(records),
        "stopped": _stopped(records),
    }
