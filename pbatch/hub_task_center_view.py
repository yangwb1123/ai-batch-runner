"""Pure Hub-native session/turn projection into the shared read-only task schema."""
from __future__ import annotations

from collections import Counter

from .hub_task_center_store import source_key
from .hub_store_helpers import boolean, identifier, bounded_text
from .task_attention import attention_view
from .task_center_adapters import identity
from .task_center_io import display
from .task_center_metrics import number, usage_metrics, efficiency
from .task_billing import billing_metrics

METHOD = "hub-native-task-facts-v1"
UNTRACKED_GROUPS = ("goals", "intents", "repairs", "resources", "feedback")
CAPABILITIES = {"control": False, "resume": False, "merge": False, "cleanup": False,
                "feedback_upload": False, "budget_or_deadline_reset": False}


def _source(tenant: str, row: dict, raw: dict) -> dict:
    instance = identifier(row["instance_id"], "instance")
    key = source_key(tenant, instance)
    return {"source_id": key, "name": key, "project": display(instance, 80),
        "origin": "authenticated_hub_instance_metadata", "workspace": None, "data_dir": None,
        "hub": {"tenant_id": tenant, "instance_id": instance,
                "reported_generation": _generation(row["generation"]),
                "last_heartbeat_at": _timestamp(row["last_seen"]), "liveness": "not_verified"},
        "coverage": [{"feed": "hub_" + kind, "truncated": raw[kind + "_partial"],
                       "incomplete": raw[kind + "_partial"], "malformed": 0, "error": ""}
                      for kind in ("sessions", "turns")]}


def _generation(value) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("invalid recorded Hub generation")
    return value


def _timestamp(value) -> float:
    result = number(value)
    if result is None:
        raise ValueError("invalid recorded Hub timestamp")
    return result


def _candidate(tenant: str, source: dict, value: dict, kind: str, partial: bool) -> tuple:
    session = identifier(value["session_id"], "session")
    turn = identifier(value["turn_id"], "turn") if kind == "hub_turn" else ""
    name = display(bounded_text(value["name"], "name", 256), 240)
    project = display(bounded_text(value["project_id"], "project", 256), 240)
    status = bounded_text(value["status"], "status", 32)
    generation, stamp = _generation(value["generation"]), _timestamp(value["updated_at"])
    if generation > source["hub"]["reported_generation"]:
        raise ValueError("Hub task generation conflicts with admitted instance metadata")
    frozen = boolean(bool(value["frozen"]) if type(value["frozen"]) is int and value["frozen"] in (0, 1)
                     else value["frozen"], "frozen")
    key = identity([METHOD, tenant, source["hub"]["instance_id"], kind, session, turn])
    hub = {"tenant_id": tenant, "instance_id": source["hub"]["instance_id"], "session_id": session,
           "turn_id": turn or None, "reported_generation": generation,
           "session_frozen": frozen, "project_label": project}
    if kind == "hub_turn":
        hub["registered_at"] = _timestamp(value["created_at"])
        if hub["registered_at"] > stamp or status not in {
                "queued", "running", "cancel_requested", "completed", "failed", "cancelled", "interrupted", "lost"}:
            raise ValueError("invalid recorded Hub turn state/time")
    row = {"key": key, "kind": kind, "run_id": session, "local_task_id": turn or session,
           "title": name, "status": status, "phase": "hub_recorded_state", "ts": stamp,
           "elapsed": None, "started_at": None, "usage": None,
           "record_ref": identity([METHOD, key, generation, stamp, status]), "hub": hub}
    return stamp, key, source, [row], partial


def _task(candidate: tuple, parent_keys: set) -> dict:
    _, key, source, rows, partial = candidate
    row = rows[-1]
    usage = usage_metrics(rows, partial)
    usage.update(calls=None, calls_basis="untracked_hub_turn_is_not_a_provider_or_cli_call")
    parent = ("tc-" + identity([METHOD, row["hub"]["tenant_id"], row["hub"]["instance_id"],
                               "hub_session", row["run_id"], ""])) if row["kind"] == "hub_turn" else None
    return {"task_id": "tc-" + key, "source_id": source["source_id"], "project": source["project"],
        "kind": row["kind"], "title": row["title"], "run_id": row["run_id"],
        "local_task_id": row["local_task_id"], "status": row["status"], "phase": row["phase"],
        "hub": row["hub"], "goal": {"statement": None, "origin": "untracked_session_label_is_not_user_goal",
                                      "acceptance": "untracked", "inferred_goal": None}, "goal_links": [],
        "time": {"started_at": None, "last_observed_at": row["ts"], "elapsed_wall_seconds": None,
                 "reported_work_seconds": None, "waiting_seconds": None, "remaining_seconds": None,
                 "liveness": "not_verified", "forecast_basis": "untracked"},
        "flow": {"previous_observed_stage": None, "previous_observed_status": None,
                 "last_observed_stage": None, "next_stage": None, "next_status": "untracked",
                 "note": "Hub state metadata is not a stage plan or remaining DAG"},
        "lineage": {"parent_task_id": parent, "children": [], "children_window_only": True,
                    "resolution": ("observed_in_window" if parent in parent_keys else "unavailable_in_window")
                        if parent else "untracked", "origin": "explicit_hub_session_turn_link" if parent else "untracked"},
        "plan": None, "llm": [], "usage": usage, "billing": billing_metrics(rows, partial),
        "efficiency": efficiency(usage), "forecast": {"duration_range_seconds": None, "remaining_seconds": None,
                                                       "basis": "untracked_hub_execution_profile"},
        "artifacts": [], "delivery": {"declared_files": None, "present_files": None, "acceptance": "untracked"},
        "analysis": {"method": METHOD, "failure_observations": int(row["status"] == "failed"), "flags": [],
                     "latest_reason": "", "partial_window": partial},
        "observations": [{"record_ref": row["record_ref"], "feed": "hub_metadata", "ts": row["ts"],
                          "status": row["status"], "stage": "", "phase": row["phase"], "attempt": None}],
        "capabilities": dict(CAPABILITIES)}


def _link_children(tasks):
    by_key = {row["task_id"]: row for row in tasks}
    for row in tasks:
        parent = by_key.get(row["lineage"]["parent_task_id"])
        if parent:
            parent["lineage"]["children"].append(row["task_id"])


def _notes(partial, source, sources):
    notes = ["Hub native session/turn metadata only; local ledgers, compute, goals, histories, repair, retention and feedback are untracked"]
    if partial:
        notes.append("Hub metadata read window is partial; omitted database totals are unknown")
    if source and not sources:
        notes.append("Hub task source unavailable in this authorized window; not proof of deletion or resolved work")
    return notes


def project(tenant: str, raw: dict, limit: int, source: str = "") -> dict:
    sources = [_source(tenant, row, raw) for row in raw["sources"]]
    source_map = {row["hub"]["instance_id"]: row for row in sources}
    partial = any(raw[key + "_partial"] for key in ("sources", "sessions", "turns"))
    candidates = [_candidate(tenant, source_map[row["instance_id"]], row, "hub_" + kind, partial)
                  for kind, values in (("session", raw["sessions"]), ("turn", raw["turns"])) for row in values]
    candidates.sort(key=lambda value: (value[0], value[1]), reverse=True)
    selected = candidates[:limit]
    keys = {"tc-" + row[1] for row in selected}
    tasks = [_task(row, keys) for row in selected]
    _link_children(tasks)
    notes = _notes(partial, source, sources)
    view = {"schema_version": 1, "generated_at": None, "window_only": True, "sources": sources, "tasks": tasks,
            **{kind: [] for kind in UNTRACKED_GROUPS}, "diagnostics": notes,
            "observed_task_count": len(candidates), "omitted_tasks": len(candidates) - len(tasks),
            "status_counts": dict(Counter(row["status"] for row in tasks)),
            "activity": {"processes": [], "diagnostics": ["remote process/lease liveness untracked"]},
            "scope": {"method": METHOD, "tenant_id": tenant, "authorization": "hub_user_resource_grant_per_request",
                      "required_scopes": ["agent.instances:read", "agent.sessions:read"],
                      "source_filter": source, "partial_window": partial, "complete_inventory": False,
                      "omitted_database_records": None if partial else 0,
                      "metadata_groups": {kind: "untracked_not_imported" for kind in UNTRACKED_GROUPS},
                      "compute": "untracked_separate_scope_required", "grants_execution": False}}
    view["attention"] = attention_view(view, candidates)
    return view
