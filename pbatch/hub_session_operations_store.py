"""A bounded read projection over existing creation and closure ledgers."""

import base64
import binascii
import json
import math
import re

from .hub_store_helpers import allowed_instance_ids, identifier, page_limit


SCHEMA = """
CREATE INDEX IF NOT EXISTS hub_session_requests_history
  ON hub_session_requests(tenant,created_at DESC,request_id DESC);
CREATE INDEX IF NOT EXISTS hub_session_requests_instance_history
  ON hub_session_requests(tenant,instance_id,created_at DESC,request_id DESC);
CREATE INDEX IF NOT EXISTS hub_session_close_requests_history
  ON hub_session_close_requests(tenant,created_at DESC,request_id DESC);
CREATE INDEX IF NOT EXISTS hub_session_close_requests_instance_history
  ON hub_session_close_requests(tenant,instance_id,created_at DESC,request_id DESC);
"""
TABLES = {"create": "hub_session_requests", "close": "hub_session_close_requests"}
FIELDS = ("request_id", "instance_id", "name", "status", "session_id", "error_code",
          "created_at", "updated_at")


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid operation time")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError("invalid operation time") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid operation time")
    return result


def encode_cursor(created_at, operation, request_id):
    payload = [1, _time(created_at).hex(), operation, request_id]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid operation cursor")
    if not value:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid operation cursor")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, list) or len(payload) != 4:
            raise ValueError("invalid operation cursor")
        version, timestamp, operation, request_id = payload
        if type(version) is not int or version != 1 or not isinstance(timestamp, str):
            raise ValueError("invalid operation cursor")
        created_at = _time(float.fromhex(timestamp))
        if operation not in TABLES:
            raise ValueError("invalid operation cursor")
        identifier(request_id, "request_id")
        if encode_cursor(created_at, operation, request_id) != value:
            raise ValueError("noncanonical operation cursor")
        return created_at, operation, request_id
    except (binascii.Error, OverflowError, TypeError, UnicodeError, RecursionError):
        raise ValueError("invalid operation cursor") from None


def _older(operation, before, predicates, values):
    if before is None:
        return
    created_at, cursor_operation, request_id = before
    if operation == cursor_operation:
        predicates.append("(created_at,request_id) < (?,?)")
        values.extend((created_at, request_id))
    else:
        comparison = "<=" if operation < cursor_operation else "<"
        predicates.append("created_at " + comparison + " ?")
        values.append(created_at)


def _branch(conn, operation, tenant, instance, before, limit, allowed):
    predicates, values = ["tenant=?"], [tenant]
    if instance:
        predicates.append("instance_id=?")
        values.append(instance)
    if allowed is not None:
        predicates.append("instance_id IN (" + ",".join("?" for _ in allowed) + ")")
        values.extend(allowed)
    _older(operation, before, predicates, values)
    fields = ",".join("'' AS name" if operation == "close" and key == "name" else key for key in FIELDS)
    query = ("SELECT " + fields + " FROM " + TABLES[operation] + " WHERE "
             + " AND ".join(predicates) + " ORDER BY created_at DESC,request_id DESC LIMIT ?")
    rows = conn.execute(query, values + [limit + 1]).fetchall()
    return [dict(row, operation=operation) for row in rows]


def list_operations(hub, tenant, instance_id="", *, before="", limit=20, allowed_instances=None):
    tenant = identifier(tenant, "tenant")
    if not isinstance(instance_id, str):
        raise ValueError("instance_id must be text")
    instance = identifier(instance_id, "instance_id") if instance_id else ""
    limit = page_limit(limit, 50)
    cursor, allowed = decode_cursor(before), allowed_instance_ids(allowed_instances)
    if allowed == ():
        return {"data": [], "next_cursor": ""}
    conn = hub._connect()
    try:
        conn.execute("BEGIN")
        rows = [row for operation in TABLES for row in _branch(
            conn, operation, tenant, instance, cursor, limit, allowed)]
    finally:
        conn.close()
    rows.sort(key=lambda row: (_time(row["created_at"]), row["operation"], row["request_id"]), reverse=True)
    selected = rows[:limit]
    next_cursor = ""
    if len(rows) > limit:
        last = selected[-1]
        next_cursor = encode_cursor(last["created_at"], last["operation"], last["request_id"])
    return {"data": selected, "next_cursor": next_cursor}
