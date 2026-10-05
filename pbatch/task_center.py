"""Read-only global task snapshot shared by CLI and the operations HTTP view.

This first slice projects existing facts, not LLM guesses or control grants.
Unknown acceptance, ETA, ownership and liveness remain explicitly unknown.
"""
from __future__ import annotations

import stat
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .task_center_activity import activity_snapshot
from .task_center_adapters import deduplicate, read_source
from .task_center_io import checked_path, display
from .task_center_metrics import efficiency, time_metrics, usage_metrics
from .task_sources import discover_sources

SCHEMA_VERSION = 1
MAX_TASKS = 200


def snapshot(limit: int = 100, source_id: str = "", now: float | None = None) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TASKS:
        raise ValueError("task limit must be between 1 and 200")
    now = time.time() if now is None else now
    payload = {"schema_version": SCHEMA_VERSION,
               "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
               "window_only": True, "sources": [], "tasks": [], "diagnostics": [],
               "observed_task_count": 0, "omitted_tasks": 0, "status_counts": {},
               "activity": activity_snapshot() if not source_id else {"processes": [], "diagnostics": []}}
    try:
        sources, notes = discover_sources()
        payload["diagnostics"].extend(notes)
    except (OSError, ValueError) as exc:
        payload["diagnostics"].append(display(str(exc)))
        return payload
    candidates = []
    for source in sources:
        if source_id and source_id not in {source["source_id"], source["name"]}:
            continue
        candidates.extend(_source_candidates(source, payload))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    payload["observed_task_count"] = len(candidates)
    payload["omitted_tasks"] = max(0, len(candidates) - limit)
    payload["tasks"] = [_task(source, rows, partial, now)
                        for _, _, source, rows, partial in candidates[:limit]]
    payload["status_counts"] = dict(Counter(task["status"] for task in payload["tasks"]))
    if source_id and not payload["sources"]:
        payload["diagnostics"].append("task source not found")
    return payload


def _source_candidates(source: dict, payload: dict) -> list[tuple]:
    raw, coverage = read_source(source)
    payload["sources"].append({**source, "coverage": coverage})
    grouped = {}
    for row in raw:
        grouped.setdefault(row["key"], []).append(row)
    partial = any(item["truncated"] or item["malformed"] or item["error"]
                  or item["incomplete"] for item in coverage)
    candidates = []
    for key, rows in grouped.items():
        rows = deduplicate(rows)
        candidates.append((rows[-1].get("ts") or 0, key, source, rows, partial))
    return candidates


def detail(task_id: str, source_id: str = "") -> dict | None:
    """Bounded lookup in the same recent window, never a client-supplied path."""
    view = snapshot(MAX_TASKS, source_id)
    return next((task for task in view["tasks"] if task["task_id"] == task_id), None)


def _task(source: dict, rows: list[dict], partial: bool, now: float) -> dict:
    last = rows[-1]
    usage = usage_metrics(rows, partial)
    if last["kind"] == "direction":
        usage["calls"] = None  # direction records do not account for child LLM calls
    outputs = list(dict.fromkeys(path for row in rows for path in row["outputs"]))[:16]
    artifacts = [_artifact(path, source) for path in outputs]
    failures = len({(row["attempt"], row["stage"]) for row in rows
                    if row["status"].lower() in {"failed", "rejected", "pipeline_failed",
                        "gate_rejected", "validation_failed"}})
    return {
        "task_id": "tc-" + last["key"], "source_id": source["source_id"],
        "project": source["name"], "kind": last["kind"], "title": last["title"],
        "run_id": last["run_id"], "local_task_id": last["local_task_id"],
        "status": last["status"], "phase": last["phase"],
        "goal": {"statement": last["title"], "origin": "recorded_task_brief",
                 "acceptance": "untracked", "inferred_goal": None},
        "time": time_metrics(rows, now), "flow": _flow(rows),
        "llm": _models(rows), "usage": usage, "efficiency": efficiency(usage),
        "artifacts": artifacts,
        "delivery": {"declared_files": len(artifacts) if any(row["output_tracking"] for row in rows)
                     else None,
                     "present_files": (sum(item["availability"] == "present" for item in artifacts)
                         if any(row["output_tracking"] for row in rows) else None),
                     "acceptance": "untracked", "branch": last.get("branch", ""),
                     "commit": last.get("commit", ""), "merge_status": last.get("merge_status", "")},
        "analysis": {"method": "observed_facts", "failure_observations": failures,
                     "flags": ["repeated_failure"] if failures >= 2 else [],
                     "latest_reason": last["reason"], "partial_window": partial},
        "observations": [{"record_ref": row["record_ref"], "feed": row["feed"],
                          "ts": row["ts"], "status": row["status"], "stage": row["stage"],
                          "phase": row["phase"], "attempt": row["attempt"]} for row in rows[-32:]],
        "capabilities": {"control": False, "cleanup": False, "feedback_upload": False},
    }


def _flow(rows: list[dict]) -> dict:
    current = rows[-1]["stage"] or None
    previous = next((row["stage"] for row in reversed(rows)
                     if row["stage"] and row["stage"] != current), None)
    previous_state = next((row["status"] for row in reversed(rows)
                           if row["stage"] == previous), None) if previous else None
    return {"last_observed_stage": current,
            "previous_observed_stage": previous, "previous_observed_status": previous_state,
            "next_stage": None, "next_status": "untracked",
            "note": "stage observations are not a remaining execution plan"}


def _models(rows: list[dict]) -> list[dict]:
    combinations = dict.fromkeys((row["agent"], row["provider"], row["model"], row["thinking"])
                                 for row in rows if row["agent"] or row["model"])
    return [{"agent": agent or None, "provider": provider or None, "model": model or None,
             "thinking": thinking or None, "origin": "recorded_configuration"}
            for agent, provider, model, thinking in list(combinations)[-8:]]


def _artifact(value: str, source: dict) -> dict:
    result = {"path": display(value, 2048), "availability": "unresolved", "bytes": None,
              "validation": "not_reverified"}
    path = Path(value)
    if not path.is_absolute() and not source["workspace"]:
        return result
    if not path.is_absolute():
        path = Path(source["workspace"]) / path
    try:
        path = checked_path(path)
        roots = [Path(source["data_dir"])]
        if source["workspace"]:
            roots.append(Path(source["workspace"]))
        if not any(path == root or root in path.parents for root in roots):
            result["availability"] = "outside_registered_roots"
            return result
        info = path.stat()
        result["availability"] = "present" if stat.S_ISREG(info.st_mode) else "not_a_file"
        result["bytes"] = info.st_size if stat.S_ISREG(info.st_mode) else None
    except FileNotFoundError:
        result["availability"] = "missing"
    except (OSError, ValueError):
        result["availability"] = "unavailable"
    return result
