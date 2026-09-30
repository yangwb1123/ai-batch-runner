"""Read-only progress view over the private runtime ledgers, per workspace.

This answers the question an operator actually has when several campaign
instances are running: *is any of them stuck?* It reads only what the runtime
already writes — the pipeline receipt ledger and the isolate session ledger —
and it never writes, schedules, or claims ownership of anything.

What it deliberately does not do:

* **Guess at progress.** A receipt lands when a stage *completes*, so a gap
  between two receipts is ambiguous: a long stage and a crashed process look
  identical. Idle time is therefore reported as ``idle_seconds``, and only
  combined with independent evidence — an unfinished session recorded by the
  isolate ledger — does it raise a flag.
* **Invent a rate.** Every count is accompanied by the number of records it
  came from, and a workspace with no receipts is reported as ``None`` rather
  than as zeros, so "nothing ran" never reads like "everything passed".

The payload is versioned because it is meant to be projected into more than one
surface; a consumer can tell what it is looking at.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import log
from .user_dirs import pbatch_runtime_home

INSTANCE_VIEW_SCHEMA_VERSION = 1
MAX_WORKSPACES = 200
MAX_RECEIPTS_PER_WORKSPACE = 500
MAX_SESSION_RECORDS = 200

RECEIPTS_RELATIVE = ("pipeline", "receipts.jsonl")
SESSION_LEDGER = "sessions.jsonl"
RECEIPT_SCHEMA_VERSION = 1

#: Session states that mean the work is still in flight. Anything else —
#: MERGED, ABORTED, DIRTY, CONFLICT, TEST_FAILED, NO_GATE, QUEUE_REFUSED,
#: READY — is finished, one way or another, so silence after it is expected.
UNFINISHED_SESSION_STATES = frozenset(
    {"CREATED", "RUNNING", "REBASING", "VALIDATING", "MERGING"})

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def projects_root() -> Path:
    """Root holding one private runtime directory per workspace."""
    return pbatch_runtime_home() / "projects"


def instance_rows(stale_seconds: int = 900,
                  limit: int = MAX_WORKSPACES) -> dict:
    """One row per workspace runtime directory, plus the rule that flagged it."""
    root = projects_root()
    rows = []
    for project in _project_dirs(root, limit):
        row = _row(project, stale_seconds)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda item: (-int(item["idle_seconds"] or 0), item["workspace"]))
    return {
        "schema_version": INSTANCE_VIEW_SCHEMA_VERSION,
        "stale_seconds": stale_seconds,
        "flag_rule": ("unfinished isolate session with no completed stage for "
                      "stale_seconds, or two or more consecutive failed stages"),
        "workspaces": rows,
        "unavailable": None if root.is_dir() else str(root),
    }


def _project_dirs(root: Path, limit: int) -> list[Path]:
    if not root.is_dir():
        return []
    try:
        found = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError as exc:
        log.debug("instance view: cannot enumerate %s (%s)", root, exc)
        return []
    return found[:max(0, int(limit))]


def _row(project: Path, stale_seconds: int) -> Optional[dict]:
    receipts = _read_jsonl(project.joinpath(*RECEIPTS_RELATIVE),
                           MAX_RECEIPTS_PER_WORKSPACE, RECEIPT_SCHEMA_VERSION)
    if not receipts and not (project / SESSION_LEDGER).exists():
        return None
    latest = _receipt_view(receipts)
    state, unfinished = _session_view(project)
    idle = _idle_seconds(latest["last_activity"])
    row = {
        "workspace": project.name,
        "stages": latest["stages"],
        "passes": latest["passes"],
        "failures": latest["failures"],
        "cited_stages": latest["cited_stages"],
        "consecutive_failures": latest["consecutive_failures"],
        "last_stage": latest["last_stage"],
        "last_activity": latest["last_activity"],
        "idle_seconds": idle,
        "session_state": state,
        "session_unfinished": unfinished,
        "receipt_records": len(receipts),
        "malformed_records": latest["malformed_records"],
        "flag": "",
        "flag_reason": "",
    }
    row["flag"], row["flag_reason"] = _flag(row, stale_seconds)
    return row


def _receipt_view(receipts: list[dict]) -> dict:
    """Counts and latest stage over the bounded receipt window.

    A record that carries the schema version but no stage name is not evidence
    that a stage finished, so it is not counted as one — inflating activity
    would defeat the purpose of a view meant to spot activity that stopped. The
    raw record count is still reported alongside, so a malformed ledger is
    visible rather than quietly discarded.
    """
    stages = passes = failures = cited = 0
    malformed = 0
    last_stage = last_activity = ""
    consecutive = 0
    for record in receipts:
        if not str(record.get("stage") or ""):
            malformed += 1
            continue
        stages += 1
        if record.get("ok"):
            passes += 1
            consecutive = 0
        else:
            failures += 1
            consecutive += 1
        if int(record.get("knowledge_cited") or 0):
            cited += 1
        stamp = str(record.get("ts") or "")
        if stamp >= last_activity:
            last_activity = stamp
            last_stage = str(record.get("stage") or "")
    return {"stages": stages, "passes": passes, "failures": failures,
            "cited_stages": cited, "consecutive_failures": consecutive,
            "malformed_records": malformed,
            "last_stage": last_stage, "last_activity": last_activity}


def _session_view(project: Path) -> tuple[Optional[str], bool]:
    """Latest isolate session state, and whether work is still in flight."""
    records = _read_jsonl(project / SESSION_LEDGER, MAX_SESSION_RECORDS)
    for record in records:
        state = str(record.get("status") or record.get("state") or "")
        if state:
            return state, state in UNFINISHED_SESSION_STATES
    return None, False


def _flag(row: dict, stale_seconds: int) -> tuple[str, str]:
    """Flag only on evidence, never on idle time alone.

    Idle time by itself is not evidence: a stage that legitimately runs long
    produces the same gap as a crashed process. The idle check therefore also
    requires an unfinished session from an independent ledger, and repeated
    verified failures stand on their own.
    """
    if row["consecutive_failures"] >= 2:
        return "consecutive_failures", (
            f"{row['consecutive_failures']} consecutive failed stages")
    idle = row["idle_seconds"]
    if row["session_unfinished"] and idle is not None and idle > stale_seconds:
        return "silent_unfinished_session", (
            f"session {row['session_state']} unfinished, "
            f"nothing completed for {idle}s")
    return "", ""


def _idle_seconds(stamp: str) -> Optional[int]:
    if not stamp:
        return None
    try:
        moment = datetime.strptime(stamp, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))


def _read_jsonl(path: Path, limit: int,
                schema_version: Optional[int] = None) -> list[dict]:
    """Bounded tail read of a JSONL ledger; unreadable means empty, not error.

    ``schema_version`` filters when the caller owns a versioned format. It must
    stay None for ledgers that carry their own version (the isolate session
    ledger does), or every record is silently dropped and the view reports an
    empty state it never observed.
    """
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-max(0, limit):]
    except OSError as exc:
        log.debug("instance view: cannot read %s (%s)", path, exc)
        return []
    records = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        if schema_version is not None and value.get("schema_version") != schema_version:
            continue
        records.append(value)
    return records
