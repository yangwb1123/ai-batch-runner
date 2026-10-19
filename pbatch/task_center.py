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
from .task_center_adapters import deduplicate, identity, read_source
from .task_center_io import checked_path, display
from .task_center_metrics import efficiency, time_metrics, usage_metrics
from .task_sources import discover_sources
from .task_goal_view import attach_goals, goal_views
from .task_plan_view import plan_flow, public_plan
from .task_intent_view import attach_intents, intent_views
from .task_duration import build_priors, forecast_for
from .task_billing import billing_metrics
from .task_consumption import attach_consumption
from .task_execution_map import execution_mapping
from .task_repair_preview import attach_repair_previews
from .task_repair_view import attach_repairs, repair_views
from .task_retention_view import attach_resources, resource_views
from .task_preservation import preservation_views
from .task_feedback_view import feedback_views
from .task_attention import attention_view

SCHEMA_VERSION = 1
MAX_TASKS = 200


def snapshot(limit: int = 100, source_id: str = "", now: float | None = None) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TASKS:
        raise ValueError("task limit must be between 1 and 200")
    now = time.time() if now is None else now
    payload = {"schema_version": SCHEMA_VERSION,
               "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
               "window_only": True, "sources": [], "tasks": [], "goals": [], "intents": [], "repairs": [],
               "resources": [], "feedback": [], "diagnostics": [],
               "observed_task_count": 0, "omitted_tasks": 0, "status_counts": {},
               "execution_mapping": execution_mapping([], partial=True),
               "activity": activity_snapshot() if not source_id else {"processes": [], "diagnostics": []}}
    try:
        sources, notes = discover_sources()
        payload["diagnostics"].extend(notes)
    except (OSError, ValueError) as exc:
        payload["diagnostics"].append(display(str(exc)))
        payload["attention"] = attention_view(payload, [])
        return payload
    candidates = []
    for source in sources:
        if source_id and source_id not in {source["source_id"], source["name"]}:
            continue
        candidates.extend(_source_candidates(source, payload))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    payload["observed_task_count"] = len(candidates)
    payload["omitted_tasks"] = max(0, len(candidates) - limit)
    priors = build_priors(candidates)
    payload["tasks"] = [_task(source, rows, partial, now, priors)
                        for _, _, source, rows, partial in candidates[:limit]]
    payload["execution_mapping"] = execution_mapping(
        [row for _, _, _, rows, _ in candidates for row in rows],
        partial=any(partial for _, _, _, _, partial in candidates))
    _enrich(payload, candidates, now)
    payload["status_counts"] = dict(Counter(task["status"] for task in payload["tasks"]))
    if source_id and not payload["sources"]:
        payload["diagnostics"].append("task source not found")
    payload["attention"] = attention_view(payload, candidates)
    return payload


def _enrich(payload: dict, candidates: list, now: float) -> None:
    tasks = payload["tasks"]
    parents = {(task["source_id"], task["run_id"]): task for task in tasks
               if task["kind"] == "pipeline"}
    for task in tasks:
        lineage = task["lineage"]
        key = (lineage["parent_source_id"], lineage["parent_run_id"])
        if key in parents:
            lineage["resolution"] = "observed_in_window"
            parents[key]["lineage"]["children"].append(task["task_id"])
    payload["goals"], notes = goal_views(payload["sources"])
    payload["diagnostics"].extend(notes)
    attach_goals(tasks, payload["goals"])
    payload["intents"], notes = intent_views(payload["sources"], payload["goals"])
    payload["diagnostics"].extend(notes)
    consumption = attach_consumption(tasks, payload["goals"], payload["intents"], candidates, payload["sources"])
    attach_intents(tasks, payload["goals"], payload["intents"])
    attach_repair_previews(tasks, payload["goals"], candidates)
    payload["repairs"], notes = repair_views(payload["sources"], payload["goals"], candidates, now, consumption)
    payload["diagnostics"].extend(notes)
    attach_repairs(tasks, payload["goals"], payload["repairs"])
    payload["resources"], notes = resource_views(payload["sources"], payload["goals"], payload["repairs"], candidates, consumption, now)
    payload["diagnostics"].extend(notes)
    payload["diagnostics"].extend(preservation_views(payload["resources"]))
    attach_resources(tasks, payload["goals"], payload["resources"])
    payload["feedback"], notes = feedback_views(payload["sources"])
    payload["diagnostics"].extend(notes)


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
        conflict = any(isinstance(row.get("usage"), dict) and row["usage"].get("_conflicts") for row in rows)
        if conflict:
            payload["diagnostics"].append("conflicting duplicate usage observations: tc-" + key)
        candidates.append((rows[-1].get("ts") or 0, key, source, rows, partial or conflict))
    return candidates


def detail(task_id: str, source_id: str = "") -> dict | None:
    """Bounded lookup in the same recent window, never a client-supplied path."""
    view = snapshot(MAX_TASKS, source_id)
    return next((task for task in view["tasks"] if task["task_id"] == task_id), None)


def _task(source: dict, rows: list[dict], partial: bool, now: float, priors: dict) -> dict:
    last = rows[-1]
    usage = usage_metrics(rows, partial)
    if last["kind"] in {"direction", "pipeline"}:
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
        "time": time_metrics(rows, now), "flow": (plan_flow(last["checkpoint"])
                if last["kind"] == "pipeline" else _flow(rows)),
        "lineage": _lineage(last), "goal_links": [],
        "plan": public_plan(last["checkpoint"]) if last["kind"] == "pipeline" else None,
        "llm": _models(rows), "usage": usage, "billing": billing_metrics(rows, partial),
        "efficiency": efficiency(usage),
        "forecast": forecast_for(source["source_id"], last, priors),
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


def _lineage(row: dict) -> dict:
    parent_run, parent_source = row.get("parent_run_id", ""), row.get("parent_source_id", "")
    parent_key = identity([parent_source, "pipeline", parent_run]) if parent_run and parent_source else None
    return {"parent_run_id": parent_run or None, "parent_source_id": parent_source or None,
            "parent_task_id": "tc-" + parent_key if parent_key else None, "children": [],
            "children_window_only": True,
            "resolution": "unavailable_in_window" if parent_key else "untracked",
            "origin": "recorded_runtime_link" if parent_key else "untracked"}


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
