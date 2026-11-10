"""Bounded authorized task discovery over session and latest-turn facts."""

import base64
import binascii
import hashlib
import json
import re
import sqlite3
from time import monotonic

from .hub_errors import HubAccessError
from .hub_store_helpers import HubStoreError, bounded_text, identifier, session_record
from .hub_turn_history import _stamp

CATEGORIES = frozenset({"active", "closed", "attention", "completed", "idle"})
QUERY_SECONDS = 5.0
QUERY_VM_STEPS = 1_000_000
_PROGRESS_STEPS = 1000
_CATEGORY = """CASE
 WHEN EXISTS(SELECT 1 FROM hub_turns a WHERE a.tenant=s.tenant AND a.session_id=s.session_id
             AND a.status IN ('queued','running','cancel_requested'))
      OR s.status IN ('queued','running','cancel_requested','closing') THEN 'active'
 WHEN s.status='closed' THEN 'closed'
 WHEN s.status IN ('failed','lost','interrupted','cancelled','uncertain','close_failed','close_lost')
      OR t.status IN ('failed','cancelled','interrupted','lost') THEN 'attention'
 WHEN t.status='completed' THEN 'completed'
 ELSE 'idle' END"""
_SELECT = """SELECT s.*,t.turn_id AS latest_id,t.status AS latest_state,
 t.created_at AS latest_created,t.updated_at AS latest_updated,""" + _CATEGORY + """ AS category
 FROM hub_sessions s LEFT JOIN hub_turns t ON t.tenant=s.tenant AND t.session_id=s.session_id
 AND t.turn_id=(SELECT last.turn_id FROM hub_turns last
               WHERE last.tenant=s.tenant AND last.session_id=s.session_id
               ORDER BY last.created_at DESC,last.turn_id DESC LIMIT 1)
 WHERE """


def dispatch(service, method, query, body, principal):
    if method != "GET":
        raise HubAccessError("method_not_allowed", "task sessions are read-only", 405)
    if not isinstance(body, dict) or body:
        raise ValueError("task listing does not accept a body")
    principal.require("agent.sessions:read")
    options = _options(query)
    if options["instance_id"]:
        principal.require("agent.sessions:read", options["instance_id"])
    binding = _binding(principal, options)
    after = _decode(options["after"], binding)
    rows = _read(service.store, principal, options, after)
    selected, limit = rows[:options["limit"]], options["limit"]
    cursor = _encode(binding, selected[-1]["session_id"]) if len(rows) > limit else ""
    return 200, {"data": selected, "next_cursor": cursor}


def _options(query):
    if not isinstance(query, dict) or set(query) - {"limit", "after", "instance_id", "state", "q"}:
        raise ValueError("unsupported task listing query")
    limit = query.get("limit", "20")
    if not isinstance(limit, str) or not re.fullmatch(r"[0-9]{1,2}", limit) or not 1 <= int(limit) <= 50:
        raise ValueError("task listing limit must be 1..50")
    state = query.get("state", "all")
    if not isinstance(state, str) or state not in CATEGORIES | {"all"}:
        raise ValueError("invalid task category")
    search = bounded_text(query.get("q", ""), "search", 128)
    if len(search.encode("utf-8")) > 512 or any(ord(ch) < 32 or ord(ch) == 127 for ch in search):
        raise ValueError("invalid task search")
    instance = query.get("instance_id", "")
    if instance != "":
        instance = identifier(instance, "instance_id")
    return {"limit": int(limit), "after": query.get("after", ""),
            "instance_id": instance, "state": state, "q": search.casefold()}


def _binding(principal, options):
    identity = [principal.tenant_id, principal.subject, principal.client_id, sorted(principal.instances)]
    filters = [options["instance_id"], options["state"], options["q"]]
    return tuple(hashlib.sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()
                 for value in (identity, filters))


def _encode(binding, session_id):
    raw = json.dumps([1, *binding, session_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value, binding):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid task cursor")
    if not value:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid task cursor")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) != 4:
            raise ValueError("invalid task cursor")
        version, authority, filters, session_id = data
        if type(version) is not int or version != 1:
            raise ValueError("invalid task cursor version")
        if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
               for value in (authority, filters)):
            raise ValueError("invalid task cursor binding")
        if authority != binding[0]:
            raise HubAccessError("task_cursor_authority_changed", "task cursor authority changed", 403)
        if filters != binding[1]:
            raise ValueError("task cursor does not match its filters")
        identifier(session_id, "task cursor")
        if _encode(binding, session_id) != value:
            raise ValueError("noncanonical task cursor")
        return session_id
    except (binascii.Error, UnicodeError, RecursionError, TypeError):
        raise ValueError("invalid task cursor") from None


def _query(principal, options, after):
    predicates, values = ["s.tenant=?", "s.session_id>?"], [principal.tenant_id, after]
    allowed = principal.instance_filter
    if allowed is not None:
        predicates.append("s.instance_id IN (" + ",".join("?" for _ in allowed) + ")")
        values.extend(allowed)
    if options["instance_id"]:
        predicates.append("s.instance_id=?")
        values.append(options["instance_id"])
    if options["q"]:
        predicates.append("(instr(task_fold(s.name),?)>0 OR instr(task_fold(s.project_id),?)>0 "
                          "OR instr(task_fold(s.session_id),?)>0)")
        values.extend([options["q"]] * 3)
    if options["state"] != "all":
        predicates.append("(" + _CATEGORY + ")=?")
        values.append(options["state"])
    sql = _SELECT + " AND ".join(predicates) + " ORDER BY s.session_id ASC LIMIT ?"
    return sql, [*values, options["limit"] + 1]


class _ReadBudget:
    def __init__(self):
        self.deadline = monotonic() + QUERY_SECONDS
        self.steps = 0

    def expired(self):
        return self.steps >= QUERY_VM_STEPS or monotonic() >= self.deadline

    def progress(self):
        self.steps += _PROGRESS_STEPS
        return int(self.expired())

    def check(self):
        if self.expired():
            raise HubStoreError("task_query_budget", "task listing read budget exceeded", 503)


def _fold(value):
    return bounded_text(value, "stored task metadata", 256).casefold()


def _read(hub, principal, options, after):
    if principal.instance_filter == ():
        return []
    budget = _ReadBudget()
    conn = hub._connect()
    try:
        budget.check()
        conn.execute("PRAGMA busy_timeout=100")
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(budget.progress, _PROGRESS_STEPS)
        conn.create_function("task_fold", 1, _fold, deterministic=True)
        conn.execute("BEGIN")
        sql, values = _query(principal, options, after)
        rows = conn.execute(sql, values).fetchmany(options["limit"] + 1)
        budget.check()
        now, result = float(hub.clock()), []
        for row in rows:
            budget.check()
            result.append({**session_record(conn, row, now, hub.offline_after),
                           "category": row["category"], "latest_turn": _latest(row)})
        budget.check()
        return result
    except sqlite3.OperationalError:
        budget.check()
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def _latest(row):
    if row["latest_id"] is None:
        return None
    state = row["latest_state"]
    if state not in {"queued", "running", "cancel_requested", "completed", "failed", "cancelled", "interrupted", "lost"}:
        raise ValueError("invalid latest turn state")
    created, updated = _stamp(row["latest_created"]), _stamp(row["latest_updated"])
    if updated < created:
        raise ValueError("invalid latest turn time order")
    return {"turn_id": identifier(row["latest_id"], "turn"), "state": state,
            "created_at": created, "updated_at": updated}


def is_path(path):
    from urllib.parse import unquote, unquote_to_bytes
    from .hub_service import PREFIX
    if unquote(path) != PREFIX + "/task-sessions":
        return False
    unquote_to_bytes(path).decode("utf-8")
    return True


def validate_read_headers(path, headers):
    if not is_path(path):
        return
    lengths = headers.get_all("Content-Length", [])
    if (len(lengths) > 1 or any(value != "0" for value in lengths)
            or any(headers.get_all(name, []) for name in ("Transfer-Encoding", "Idempotency-Key", "If-Match"))):
        raise ValueError("task listing GET does not accept a body or mutation headers")
