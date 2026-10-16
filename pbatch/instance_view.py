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

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .instance_observation_io import project_window, read_window, window_clear
from .instance_session_view import UNFINISHED_SESSION_STATES, session_facts
from .user_dirs import pbatch_runtime_home

INSTANCE_VIEW_SCHEMA_VERSION = 1
MAX_WORKSPACES = 200
MAX_RECEIPTS_PER_WORKSPACE = 500
MAX_SESSION_RECORDS = 200

RECEIPTS_RELATIVE = ("pipeline", "receipts.jsonl")
SESSION_LEDGER = "sessions.jsonl"
RECEIPT_SCHEMA_VERSION = 1

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def projects_root() -> Path:
    """Root holding one private runtime directory per workspace."""
    return pbatch_runtime_home() / "projects"


def instance_rows(stale_seconds: int = 900,
                  limit: int = MAX_WORKSPACES) -> dict:
    """One row per workspace runtime directory, plus the rule that flagged it."""
    if type(stale_seconds) is not int or stale_seconds <= 0:
        raise ValueError("stale seconds must be a positive integer")
    if type(limit) is not int or not 0 <= limit <= MAX_WORKSPACES:
        raise ValueError("workspace limit must be between 0 and 200")
    root = projects_root()
    projects, coverage = project_window(root, limit)
    rows = []
    for project in projects:
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
        "unavailable": None if coverage["available"] else str(root),
        "coverage": coverage,
        "complete_inventory": False,
        "scope": "bounded_workspace_ledger_metadata_not_process_or_lifetime_inventory",
        "grants_execution": False,
    }


def _project_dirs(root: Path, limit: int) -> list[Path]:
    return project_window(root, limit)[0]


def _row(project: Path, stale_seconds: int) -> Optional[dict]:
    receipts, receipt_window = read_window(project.joinpath(*RECEIPTS_RELATIVE),
                                           MAX_RECEIPTS_PER_WORKSPACE)
    records, session_window = read_window(project / SESSION_LEDGER, MAX_SESSION_RECORDS)
    if receipt_window["status"] == session_window["status"] == "missing":
        return None
    latest = _receipt_view(receipts, receipt_window)
    sessions = session_facts(records, session_window)
    count = sum(_receipt_schema(record) for record in receipts)
    if not count:
        for key in ("stages", "passes", "failures", "cited_stages", "consecutive_failures"):
            latest[key] = None
    if receipt_window["incomplete"]:
        latest.update(last_stage="", last_activity="", consecutive_failures=None)
    row = {"workspace": project.name, **latest, **sessions,
           "idle_seconds": _idle_seconds(latest["last_activity"]),
           "receipt_records": count, "receipt_window": receipt_window,
           "session_window": session_window,
           "observation_complete": (window_clear(receipt_window) and window_clear(session_window)
                                    and bool(count) and bool(sessions["sessions_observed"])),
           "liveness": "not_verified", "grants_execution": False,
           "flag": "", "flag_reason": ""}
    row["flag"], row["flag_reason"] = _flag(row, stale_seconds)
    return row


def _receipt_schema(record: object) -> bool:
    return (isinstance(record, dict) and type(record.get("schema_version")) is int
            and record["schema_version"] == RECEIPT_SCHEMA_VERSION)


def _receipt_record(record: object) -> bool:
    if not _receipt_schema(record):
        return False
    stage, ok, cited = record.get("stage"), record.get("ok"), record.get("knowledge_cited", 0)
    return (isinstance(stage, str) and bool(stage.strip()) and len(stage) <= 128
            and type(ok) is bool and type(cited) is int and 0 <= cited <= 10**9)


def _receipt_view(receipts: list, window: Optional[dict] = None) -> dict:
    """Count recorded stages; gaps reset a sequence instead of joining failures."""
    stages = passes = failures = cited = malformed = consecutive = 0
    last_stage = last_activity = ""
    for record in receipts:
        if not _receipt_record(record):
            malformed += int(_receipt_schema(record))
            consecutive, last_stage, last_activity = 0, "", ""
            if window is not None and record is not None:
                window["malformed"] += 1
            continue
        stages += 1
        if record["ok"]:
            passes += 1
            consecutive = 0
        else:
            failures += 1
            consecutive += 1
        cited += int(bool(record.get("knowledge_cited", 0)))
        last_stage = record["stage"]
        stamp = record.get("ts")
        last_activity = stamp if isinstance(stamp, str) and len(stamp) <= 32 else ""
    return {"stages": stages, "passes": passes, "failures": failures,
            "cited_stages": cited, "consecutive_failures": consecutive,
            "malformed_records": malformed,
            "last_stage": last_stage, "last_activity": last_activity}


def _session_view(project: Path) -> tuple[Optional[str], bool]:
    """Compatibility projection; False means no observed unfinished session."""
    records, window = read_window(project / SESSION_LEDGER, MAX_SESSION_RECORDS)
    facts = session_facts(records, window)
    return facts["session_state"], bool(facts["session_unfinished"])


def _flag(row: dict, stale_seconds: int) -> tuple[str, str]:
    """Flag only on evidence, never on idle time alone.

    Idle time by itself is not evidence: a stage that legitimately runs long
    produces the same gap as a crashed process. The idle check therefore also
    requires an unfinished session from an independent ledger, and repeated
    verified failures stand on their own.
    """
    if (row["consecutive_failures"] or 0) >= 2:
        return "consecutive_failures", (
            f"{row['consecutive_failures']} consecutive recorded failed stages")
    idle = row["idle_seconds"]
    usable = _suffix_clear(row["receipt_window"]) and _suffix_clear(row["session_window"])
    if usable and row["session_unfinished"] and idle is not None and idle > stale_seconds:
        return "silent_unfinished_session", (
            f"{len(row['unfinished_sessions'])} observed unfinished isolate session(s), "
            f"no recorded completed stage for {idle}s; not proof of a stuck process")
    return "", ""


def _suffix_clear(window: dict) -> bool:
    """Truncation limits coverage, but cannot negate a positive retained fact."""
    return (window["available"] and not window["malformed"]
            and not window["incomplete"] and not window["error"])


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
    """Compatibility list projection; row observation also retains coverage/gaps."""
    records, _window = read_window(path, limit)
    return [value for value in records if isinstance(value, dict)
            and (schema_version is None or
                 (type(value.get("schema_version")) is int and
                  value["schema_version"] == schema_version))]
