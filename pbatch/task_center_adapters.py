"""Project existing agent, metering, and campaign facts without interpreting prose."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .task_center_io import display, read_rows
from .task_center_metrics import number, timestamp

_EVENTS = {"task_finish": "succeeded", "task_fail": "failed",
           "task_start": "running", "task_validation_pass": "succeeded",
           "task_validation_fail": "failed"}
_DIRECTION_STATES = {"SELECTED", "RUNNING", "PASSED", "PIPELINE_FAILED", "GATE_REJECTED",
                     "VALIDATION_FAILED", "MERGE_BLOCKED", "MERGED", "BUDGET_BLOCKED",
                     "PROVIDER_BREAKER_PAUSED", "RECOVERY_BLOCKED"}


def identity(parts: list) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:32]


def _text(row: dict, key: str, maximum: int = 160) -> str:
    value = row.get(key)
    return display(value, maximum) if isinstance(value, str) else ""


def _elapsed(row: dict) -> float | None:
    if "elapsed_ms" in row:
        value = number(row["elapsed_ms"])
        return value / 1000 if value is not None else None
    return number(row.get("elapsed"))


def _agent(row: dict, source: dict, feed: str) -> dict | None:
    event = _text(row, "event", 80)
    if feed == "events" and event not in _EVENTS:
        return None
    run, task = _text(row, "run_id"), _text(row, "task_id")
    stamp = row.get("ts")
    status = _EVENTS.get(event, _text(row, "status", 80)).lower()
    if not status:
        return None
    legacy = identity([stamp, _text(row, "session_id"), _text(row, "task_brief"),
                       _text(row, "stage"), _text(row, "task")])
    key = [source["source_id"], "agent", row["run_id"], row["task_id"]] if task and run else [
        source["source_id"], "legacy_agent", legacy]
    phase = _text(row, "phase", 32) or (
        "validation" if event.startswith("task_validation") else "execution")
    elapsed = _elapsed(row)
    ended = timestamp(stamp)
    started = _started(row, event, status, ended, elapsed)
    return {"key": identity(key), "kind": "agent", "run_id": run, "local_task_id": task,
            "title": _text(row, "task_brief", 240) or _text(row, "task", 240),
            "stage": _text(row, "stage") or _text(row, "stage_id"), "phase": phase,
            "status": status, "ts": ended, "started_at": started, "elapsed": elapsed,
            "attempt": int(number(row.get("attempt"), True) or 0),
            "agent": _text(row, "agent"), "provider": _text(row, "provider"),
            "model": _text(row, "model", 256), "thinking": _text(row, "thinking", 32),
            "usage": row.get("usage"), "outputs": _paths(row.get("outputs")),
            "output_tracking": isinstance(row.get("outputs"), list),
            "reason": _text(row, "reason", 500) or _text(row, "detail", 500),
            "feed": feed, "record_ref": identity([feed, key, stamp, phase, status]),
            "legacy_identity": not bool(task and run)}


def _started(row: dict, event: str, status: str, ended: float | None,
             elapsed: float | None) -> float | None:
    started = timestamp(row.get("started_at"))
    if event == "task_start" and started is None:
        started = ended
    if started is None and ended is not None and elapsed is not None and status not in {
            "running", "queued", "retrying"}:
        started = ended - elapsed
    return started


def _paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:2048] for item in value[:16] if isinstance(item, str) and item]


def _direction(row: dict, source: dict) -> dict | None:
    direction = _text(row, "direction_id")
    status = _text(row, "status", 80)
    if not direction or direction.startswith("__") or status not in _DIRECTION_STATES:
        return None
    key = [source["source_id"], "direction", _text(row, "campaign"),
           _text(row, "module"), direction]
    return {"key": identity(key), "kind": "direction", "run_id": _text(row, "run_id"),
            "local_task_id": direction, "title": _text(row, "direction", 240) or direction,
            "stage": _text(row, "stage"), "phase": "pipeline", "status": status,
            "ts": timestamp(row.get("updated_at") or row.get("ts")),
            "started_at": timestamp(row.get("updated_at")) if status == "RUNNING" else None,
            "elapsed": number(row.get("elapsed")) if status not in {"RUNNING", "SELECTED"}
                       else None,
            "attempt": _text(row, "round_id") + ":" + _text(row, "run_id"),
            "agent": "", "provider": _text(row, "provider"), "model": _text(row, "model"),
            "thinking": "", "usage": None, "outputs": [], "output_tracking": False,
            "feed": "campaign",
            "reason": _text(row, "reason", 500), "branch": _text(row, "branch", 256),
            "commit": _text(row, "commit", 80), "merge_status": _text(row, "merge_status", 80),
            "evidence": _paths(row.get("evidence")), "legacy_identity": False,
            "record_ref": identity(["campaign", key, row.get("updated_at"), status])}


def read_source(source: dict) -> tuple[list[dict], list[dict]]:
    root = Path(source["data_dir"])
    feeds = {"agents": root / "agents" / "ledger.jsonl",
             "events": Path(source["events_file"]), "campaign": Path(source["campaign_state"])}
    rows, coverage = [], []
    for feed, path in feeds.items():
        raw, info = read_rows(path)
        info["feed"] = feed
        coverage.append(info)
        for record in raw:
            adapted = _direction(record, source) if feed == "campaign" else _agent(
                record, source, feed)
            if adapted:
                rows.append(adapted)
    return rows, coverage


def deduplicate(rows: list[dict]) -> list[dict]:
    """Metering and agent ledger rows for one attempt are the same invocation."""
    unique = {}
    for row in rows:
        lane = (row["attempt"], row["stage"], row["phase"])
        if row["kind"] == "direction":
            lane = (row["record_ref"],)
        previous = unique.get(lane)
        if previous is None:
            unique[lane] = row
        else:
            preferred, fallback = sorted([previous, row], key=_rank, reverse=True)
            merged = dict(preferred)
            merged["output_tracking"] = preferred["output_tracking"] or fallback["output_tracking"]
            for field in ("usage", "title", "agent", "provider", "model", "thinking",
                          "outputs", "reason", "elapsed", "started_at"):
                if merged.get(field) is None or merged.get(field) == "" or merged.get(field) == []:
                    merged[field] = fallback.get(field)
            unique[lane] = merged
    return sorted(unique.values(), key=_rank)


def _rank(row: dict) -> tuple:
    return row.get("ts") or 0, int(row["feed"] == "agents")
