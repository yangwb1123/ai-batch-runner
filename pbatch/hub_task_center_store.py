"""Bounded metadata-only Hub reads. Tenant/instance admission precedes every LIMIT.

No prompts, event payloads, commands, provider histories, local catalogs or clock
are read. One SQLite read transaction is not a live device/filesystem snapshot.
"""
from __future__ import annotations

from .hub_store_helpers import allowed_instance_ids, identifier
from .task_center_adapters import identity

MAX_SOURCES = 64
MAX_ROWS = 512


def source_key(tenant: str, instance: str) -> str:
    return "src-" + identity(["hub-task-center-v1", tenant, instance])[:24]


def _predicate(tenant, allowed, alias=""):
    prefix = alias + "." if alias else ""
    clauses, values = [prefix + "tenant=?"], [tenant]
    if allowed is not None:
        clauses.append(prefix + "instance_id IN (" + ",".join("?" for _ in allowed) + ")")
        values.extend(allowed)
    return " AND ".join(clauses), values


def _sources(conn, tenant, allowed, source, *, key=source_key):
    where, values = _predicate(tenant, allowed)
    rows = conn.execute("SELECT CASE WHEN length(instance_id)<=256 THEN instance_id END AS instance_id,"
                        "generation,last_seen FROM hub_instances WHERE " +
                        where + " ORDER BY instance_id LIMIT ?", values + [MAX_SOURCES + 1]).fetchall()
    selected = [dict(row) for row in rows[:MAX_SOURCES]]
    # Resolve only inside this admitted source window, never crawl more instances
    # or guess an instance from a hash/name. Outside-window is not deletion.
    if source:
        selected = [row for row in selected if key(tenant, row["instance_id"]) == source]
    return selected, len(rows) > MAX_SOURCES


def _rows(conn, tenant, sources, table):
    selected = tuple(row["instance_id"] for row in sources)
    where, values = _predicate(tenant, selected, "s")
    if table == "sessions":
        query = ("SELECT s.session_id,s.instance_id,s.generation,"
                 "CASE WHEN length(s.name)<=256 THEN s.name END AS name,"
                 "CASE WHEN length(s.project_id)<=256 THEN s.project_id END AS project_id,"
                 "CASE WHEN length(s.status)<=32 THEN s.status END AS status,"
                 "s.frozen,s.updated_at FROM hub_sessions s WHERE " + where +
                 " ORDER BY s.updated_at DESC,s.session_id LIMIT ?")
    else:
        query = ("SELECT t.turn_id,t.session_id,t.instance_id,t.generation,"
                 "CASE WHEN length(t.status)<=32 THEN t.status END AS status,t.created_at,t.updated_at,"
                 "CASE WHEN length(s.name)<=256 THEN s.name END AS name,"
                 "CASE WHEN length(s.project_id)<=256 THEN s.project_id END AS project_id,s.frozen "
                 "FROM hub_turns t JOIN hub_sessions s "
                 "ON t.tenant=s.tenant AND t.session_id=s.session_id AND t.instance_id=s.instance_id "
                 "WHERE " + where + " ORDER BY t.updated_at DESC,t.turn_id LIMIT ?")
    rows = conn.execute(query, values + [MAX_ROWS + 1]).fetchall()
    return [dict(row) for row in rows[:MAX_ROWS]], len(rows) > MAX_ROWS


def read_metadata(store, tenant: str, allowed_instances, source: str = "") -> dict:
    tenant = identifier(tenant, "tenant")
    allowed = allowed_instance_ids(allowed_instances)
    empty = {"sources": [], "sessions": [], "turns": [], "sources_partial": False,
             "sessions_partial": False, "turns_partial": False}
    if allowed == ():
        return empty
    conn = store._connect()
    ticks = 0
    def budget():
        nonlocal ticks
        ticks += 1
        return int(ticks > 1200)  # bounded SQLite VM work; not a live I/O deadline
    try:
        conn.set_progress_handler(budget, 1000)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        sources, partial = _sources(conn, tenant, allowed, source)
        if not sources:
            return dict(empty, sources_partial=partial)
        sessions, sessions_partial = _rows(conn, tenant, sources, "sessions")
        turns, turns_partial = _rows(conn, tenant, sources, "turns")
        return {"sources": sources, "sessions": sessions, "turns": turns,
                "sources_partial": partial, "sessions_partial": sessions_partial,
                "turns_partial": turns_partial}
    finally:
        conn.close()
