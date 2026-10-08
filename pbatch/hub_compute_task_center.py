"""Bounded compute metadata observer; no command/result JSON, Fabric probes or dispatch.

Authorization is the existing source-instance task read grant, not placement
permission on a destination device. Registration timestamps are not execution.
"""
from __future__ import annotations

from collections import Counter
import re

from .hub_store_helpers import allowed_instance_ids, identifier
from .hub_task_center_store import MAX_ROWS, _predicate, _sources
from .hub_task_center_view import UNTRACKED_GROUPS, _generation, _timestamp, _task
from .task_attention import attention_view
from .task_center_adapters import identity
from .task_center_io import display

METHOD = "hub-compute-task-facts-v1"
PHASE = "hub_compute_recorded_state"
STATES = {"queued", "dispatching", "running", "cancel_requested", "completed", "failed", "cancelled", "lost"}


def source_key(tenant, instance):
    return "src-" + identity([METHOD, tenant, instance])[:24]


def _rows(conn, tenant, sources):
    where, values = _predicate(tenant, tuple(row["instance_id"] for row in sources))
    rows = conn.execute(
        "SELECT CASE WHEN length(task_id)<=40 THEN task_id END AS task_id,"
        "CASE WHEN length(session_id)<=256 THEN session_id END AS session_id,"
        "instance_id,generation,CASE WHEN length(state)<=32 THEN state END AS state,created_at,updated_at "
        "FROM hub_compute_tasks WHERE " + where + " ORDER BY updated_at DESC,task_id LIMIT ?",
        values + [MAX_ROWS + 1]).fetchall()
    return [dict(row) for row in rows[:MAX_ROWS]], len(rows) > MAX_ROWS


def read_metadata(store, tenant, allowed_instances, source=""):
    tenant, allowed = identifier(tenant, "tenant"), allowed_instance_ids(allowed_instances)
    empty = {"sources": [], "tasks": [], "sources_partial": False, "tasks_partial": False}
    if allowed == ():
        return empty
    conn, ticks = store._connect(), 0
    def budget():
        nonlocal ticks
        ticks += 1
        return int(ticks > 1200)
    try:
        conn.set_progress_handler(budget, 1000)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        sources, partial = _sources(conn, tenant, allowed, source, key=source_key)
        if not sources:
            return dict(empty, sources_partial=partial)
        tasks, tasks_partial = _rows(conn, tenant, sources)
        return {"sources": sources, "tasks": tasks, "sources_partial": partial, "tasks_partial": tasks_partial}
    finally:
        conn.close()


def _source(tenant, row, partial):
    instance = identifier(row["instance_id"], "instance")
    key = source_key(tenant, instance)
    return {"source_id": key, "name": key, "project": display(instance, 80),
            "origin": "authenticated_hub_compute_source_metadata", "workspace": None, "data_dir": None,
            "hub": {"tenant_id": tenant, "instance_id": instance,
                    "reported_generation": _generation(row["generation"]),
                    "last_heartbeat_at": _timestamp(row["last_seen"]), "liveness": "not_verified"},
            "coverage": [{"feed": "hub_compute_tasks", "truncated": partial, "incomplete": partial,
                          "malformed": 0, "error": ""}]}


def _candidate(tenant, source, value, partial):
    task = value["task_id"]
    if not isinstance(task, str) or not re.fullmatch(r"compute-[a-f0-9]{32}", task):
        raise ValueError("invalid recorded compute task identity")
    session = identifier(value["session_id"], "session")
    generation = _generation(value["generation"])
    stamp, registered = _timestamp(value["updated_at"]), _timestamp(value["created_at"])
    if generation > source["hub"]["reported_generation"] or registered > stamp or value["state"] not in STATES:
        raise ValueError("invalid recorded compute task state/time/generation")
    key = identity([METHOD, tenant, source["hub"]["instance_id"], session, task])
    hub = {"tenant_id": tenant, "instance_id": source["hub"]["instance_id"], "session_id": session,
           "compute_task_id": task, "reported_generation": generation, "registered_at": registered}
    row = {"key": key, "kind": "hub_compute_task", "run_id": session, "local_task_id": task,
           "title": task, "status": value["state"], "phase": PHASE, "ts": stamp,
           "elapsed": None, "started_at": None, "usage": None, "hub": hub,
           "record_ref": identity([METHOD, key, generation, stamp, value["state"]])}
    return stamp, key, source, [row], partial


def _compute_task(candidate):
    # Common shared display facts only; no native parent/goal/accounting inference.
    task = _task(candidate, set())
    task["analysis"]["method"] = METHOD
    task["goal"]["origin"] = "untracked_compute_identity_is_not_user_goal"
    task["usage"]["calls_basis"] = "untracked_compute_task_is_not_provider_or_cli_call"
    task["flow"]["note"] = "Compute state metadata is not a stage plan or remaining DAG"
    task["forecast"]["basis"] = "untracked_compute_execution_profile"
    task["lineage"]["origin"] = "recorded_hub_compute_session_reference_not_resolved"
    return task


def project(tenant, raw, limit, source=""):
    partial = raw["sources_partial"] or raw["tasks_partial"]
    sources = [_source(tenant, row, raw["tasks_partial"]) for row in raw["sources"]]
    by_instance = {row["hub"]["instance_id"]: row for row in sources}
    candidates = [_candidate(tenant, by_instance[row["instance_id"]], row, partial) for row in raw["tasks"]]
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    tasks = [_compute_task(row) for row in candidates[:limit]]
    notes = ["Hub compute metadata only; commands, results, device placement, local ledgers and native sessions are untracked"]
    if partial:
        notes.append("Hub metadata read window is partial; omitted database totals are unknown")
    if source and not sources:
        notes.append("Hub task source unavailable in this authorized window; not proof of deletion or resolved work")
    view = {"schema_version": 1, "generated_at": None, "window_only": True, "sources": sources, "tasks": tasks,
            **{kind: [] for kind in UNTRACKED_GROUPS}, "diagnostics": notes,
            "observed_task_count": len(candidates), "omitted_tasks": len(candidates) - len(tasks),
            "status_counts": dict(Counter(row["status"] for row in tasks)),
            "activity": {"processes": [], "diagnostics": ["remote process/lease liveness untracked"]},
            "scope": {"method": METHOD, "tenant_id": tenant, "authorization": "hub_user_resource_grant_per_request",
                      "required_scopes": ["agent.instances:read", "agent.tasks:read"], "source_filter": source,
                      "partial_window": bool(partial), "complete_inventory": False,
                      "omitted_database_records": None if partial else 0,
                      "metadata_groups": {kind: "untracked_not_imported" for kind in UNTRACKED_GROUPS},
                      "compute": "bounded_metadata_only_not_device_inventory_or_execution_evidence",
                      "sessions": "untracked_separate_scope_required", "grants_execution": False}}
    view["attention"] = attention_view(view, candidates)
    return view
