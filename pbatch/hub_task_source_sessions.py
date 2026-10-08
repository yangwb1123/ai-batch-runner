"""Explicit compute-to-source-session identity references, not execution/goal equivalence.

Only already admitted Hub metadata windows are read. No local ledger/history,
labels, body, command, device, billing, controller initialization or new authority.
"""
from __future__ import annotations

from collections import Counter

from .hub_store_helpers import identifier, allowed_instance_ids
from .hub_task_center_store import MAX_ROWS, _predicate, _sources, source_key as native_source_key
from .hub_task_center_view import METHOD as NATIVE_METHOD, _generation, _timestamp
from .hub_compute_task_center import _rows, source_key, project as compute_project, _candidate
from .task_attention import attention_view
from .task_center_adapters import identity

METHOD = "hub-compute-source-session-references-v1"
SCOPES = ["agent.instances:read", "agent.tasks:read", "agent.sessions:read"]
FEED = "hub_source_session_references"
BASIS = "recorded_instance_session_generation_match_only_not_execution_parent_or_equivalence"
NOTE = "Explicit source-session metadata references only; no native task inventory, local mapping, execution parent, deduplication or billing addition verified"
MATCHED = "observed_recorded_reference"
UNRESOLVED = "unresolved_in_authorized_window"


def _sessions(conn, tenant, sources):
    where, values = _predicate(tenant, tuple(row["instance_id"] for row in sources))
    rows = conn.execute(
        "SELECT CASE WHEN length(session_id)<=256 THEN session_id END AS session_id,"
        "instance_id,generation,updated_at FROM hub_sessions WHERE " + where +
        " ORDER BY updated_at DESC,session_id LIMIT ?", values + [MAX_ROWS + 1]).fetchall()
    return [dict(row) for row in rows[:MAX_ROWS]], len(rows) > MAX_ROWS


def read_metadata(store, tenant, allowed_instances, source=""):
    tenant, allowed = identifier(tenant, "tenant"), allowed_instance_ids(allowed_instances)
    empty = {"sources": [], "tasks": [], "sessions": [], "sources_partial": False,
             "tasks_partial": False, "sessions_partial": False}
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
        sessions, sessions_partial = _sessions(conn, tenant, sources)
        return {"sources": sources, "tasks": tasks, "sessions": sessions, "sources_partial": partial,
                "tasks_partial": tasks_partial, "sessions_partial": sessions_partial}
    finally:
        conn.close()


def session_index(tenant, rows, sources):
    indexed = {}
    for row in rows:
        instance = identifier(row["instance_id"], "instance")
        session = identifier(row["session_id"], "session")
        generation = _generation(row["generation"])
        if instance not in sources or generation > sources[instance]["hub"]["reported_generation"]:
            raise ValueError("source-session reference conflicts with admitted instance metadata")
        key = instance, session, generation
        if key in indexed:
            raise ValueError("ambiguous duplicate source-session reference")
        indexed[key] = {"namespace": NATIVE_METHOD, "source_id": native_source_key(tenant, instance),
            "task_id": "tc-" + identity([NATIVE_METHOD, tenant, instance, "hub_session", session, ""]),
            "tenant_id": tenant, "instance_id": instance, "session_id": session,
            "reported_generation": generation, "last_recorded_at": _timestamp(row["updated_at"])}
    return indexed


def reference(tenant, hub, target):
    key = identity([METHOD, tenant, hub["instance_id"], hub["session_id"],
                    hub["compute_task_id"], hub["reported_generation"]])
    return {"schema_version": 1, "reference_id": "hr-" + key, "basis": BASIS,
            "state": MATCHED if target is not None else UNRESOLVED, "target": target,
            "liveness": "not_verified", "parent_execution_verified": False, "goal_binding_verified": False,
            "deduplication_authority": False, "billing_addition_authority": False, "grants_execution": False}


def scope_summary(native_rows, partial, observed, matched, compute_partial):
    return {"basis": BASIS, "native_rows_observed": native_rows, "native_window_partial": partial,
            "compute_window_partial": compute_partial,
            "task_references_observed": observed, "matched_recorded_references": matched,
            "unresolved_references": observed - matched,
            "unresolved_means": "unavailable_in_admitted_window_not_absent_deleted_or_foreign_identity_proof",
            "count_basis": "all_admitted_compute_candidates_before_display_limit_not_distinct_incidents",
            "local_ledger_mapping": "untracked_not_read_or_imported", "target_native_display_presence": "not_checked",
            "parent_execution_verified": False, "deduplication_authority": False, "billing_addition_authority": False}


def decorate(view, summary):
    view["scope"].update(method=METHOD, required_scopes=list(SCOPES),
        sessions="bounded_source_session_reference_metadata_not_native_task_inventory", source_session_references=summary)
    if summary["native_window_partial"]:
        view["scope"].update(partial_window=True, omitted_database_records=None)
        marker = "Hub metadata read window is partial; omitted database totals are unknown"
        if marker not in view["diagnostics"]:
            view["diagnostics"].append(marker)
    view["diagnostics"].append(NOTE)
    for source in view["sources"]:
        source["coverage"].append({"feed": FEED, "truncated": summary["native_window_partial"],
            "incomplete": summary["native_window_partial"], "malformed": 0, "error": ""})
    return view


def format_references(view):
    summary = view["scope"]["source_session_references"]
    lines = ["源会话引用：仅记录身份/generation 的有界核对；不是执行父子关系、本地映射、去重或计费相加。",
             f"观察 {summary['task_references_observed']} 个计算引用 / 匹配 {summary['matched_recorded_references']} / 窗口内未解析 {summary['unresolved_references']}；不是缺失/删除证明。"]
    for task in view["tasks"]:
        ref = task["source_session_reference"]
        target = ref["target"]
        destination = (target["source_id"] + "/" + target["task_id"]) if target else "unavailable_in_authorized_window"
        lines.append(f"{ref['reference_id']} {ref['state']} -> {destination}")
    return "\n".join(lines)


def project(tenant, raw, limit, source=""):
    partial = raw["sources_partial"] or raw["tasks_partial"] or raw["sessions_partial"]
    view = compute_project(tenant, dict(raw, sources_partial=partial), limit, source)
    sources = {row["hub"]["instance_id"]: row for row in view["sources"]}
    native = session_index(tenant, raw["sessions"], sources)
    candidates = [_candidate(tenant, sources[row["instance_id"]], row, partial) for row in raw["tasks"]]
    refs = {}
    for candidate in candidates:
        hub = candidate[3][0]["hub"]
        target = native.get((hub["instance_id"], hub["session_id"], hub["reported_generation"]))
        refs["tc-" + candidate[1]] = reference(tenant, hub, target)
    counts = Counter(value["state"] for value in refs.values())
    summary = scope_summary(len(native), raw["sessions_partial"], len(refs), counts[MATCHED],
                            raw["sources_partial"] or raw["tasks_partial"])
    decorate(view, summary)
    for task in view["tasks"]:
        task["source_session_reference"] = refs[task["task_id"]]
    view["attention"] = attention_view(view, candidates)
    return view
