"""Read-only bounded candidate scanning for the durable turn-history projection."""

import re
import sqlite3
from time import monotonic

from .hub_store_helpers import (MAX_PROMPT_BYTES, HubStoreError, bounded_text,
                                get_session, identifier)

TURN_STATES = frozenset({"queued", "running", "cancel_requested", "completed", "failed",
                         "cancelled", "interrupted", "lost"})
QUERY_SECONDS = 5.0
QUERY_VM_STEPS = 1_000_000
QUERY_SCAN_ROWS = 5000
QUERY_SCAN_BYTES = 8 * 1024 * 1024
_PROGRESS_STEPS = 1000
_SEARCH_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def filters(q, state, turn_id, before):
    search = bounded_text(q, "turn search", 128)
    if len(search.encode("utf-8")) > 512 or _SEARCH_CONTROLS.search(search):
        raise ValueError("invalid turn search")
    if not isinstance(state, str) or state not in TURN_STATES | {"all"}:
        raise ValueError("invalid turn state filter")
    if turn_id != "":
        identifier(turn_id, "turn_id")
        if before or search or state != "all":
            raise ValueError("exact turn lookup cannot combine with history filters")
    return {"q": search.casefold(), "state": state, "turn_id": turn_id}


class ReadBudget:
    def __init__(self):
        self.deadline = monotonic() + QUERY_SECONDS
        self.steps, self.rows, self.prompt_bytes = 0, 0, 0

    def expired(self):
        return (self.steps >= QUERY_VM_STEPS or monotonic() >= self.deadline
                or self.rows > QUERY_SCAN_ROWS or self.prompt_bytes > QUERY_SCAN_BYTES)

    def progress(self):
        self.steps += _PROGRESS_STEPS
        return int(self.expired())

    def check(self):
        if self.expired():
            raise HubStoreError("turn_query_budget", "turn history read budget exceeded", 503)

    def consume(self, row):
        size = row["prompt_bytes"]
        if type(size) is not int or not 0 <= size <= MAX_PROMPT_BYTES:
            raise ValueError("invalid recorded prompt size")
        self.rows += 1
        self.prompt_bytes += size
        self.check()


def _query(tenant, session, options, cursor, limit):
    predicates, values = ["tenant=?", "session_id=?"], [tenant, session]
    if cursor:
        predicates.append("(created_at,turn_id) < (?,?)")
        values.extend(cursor)
    if options["state"] != "all":
        predicates.append("status=?")
        values.append(options["state"])
    if options["turn_id"]:
        predicates.append("turn_id=?")
        values.append(options["turn_id"])
    # No SQL prompt predicate: only the redacted, bounded returned projection is searchable.
    scan_limit = QUERY_SCAN_ROWS + 1 if options["q"] else limit + 1
    source = "hub_turns INDEXED BY hub_turns_history"
    if options["turn_id"]:
        scan_limit, source = 1, "hub_turns"
    # Keep filtered history streaming in index order instead of sorting/materializing prompt rows.
    sql = ("SELECT turn_id,session_id,instance_id,status,created_at,updated_at,"
           "length(CAST(prompt AS BLOB)) AS prompt_bytes,"
           "CASE WHEN length(CAST(prompt AS BLOB))<=? THEN prompt END AS prompt "
           "FROM " + source + " WHERE " + " AND ".join(predicates)
           + " ORDER BY created_at DESC,turn_id DESC LIMIT ?")
    return sql, [MAX_PROMPT_BYTES, *values, scan_limit]


def _scan(cursor, options, limit, budget, project):
    selected, search = [], options["q"]
    while len(selected) <= limit:
        budget.check()
        row = cursor.fetchone()
        if row is None:
            break
        budget.consume(row)
        visible = project(row)
        budget.check()
        if not search or search in visible["prompt"].casefold() or search in visible["turn_id"].casefold():
            selected.append(visible)
    budget.check()
    return selected


def read(hub, tenant, session, options, cursor, limit, project):
    budget = ReadBudget()
    conn = hub._connect()
    try:
        budget.check()
        conn.execute("PRAGMA busy_timeout=100")
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(budget.progress, _PROGRESS_STEPS)
        conn.execute("BEGIN")
        get_session(conn, tenant, session)
        sql, values = _query(tenant, session, options, cursor, limit)
        return _scan(conn.execute(sql, values), options, limit, budget, project)
    except sqlite3.OperationalError:
        budget.check()
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()
