"""Bounded, session-bound reads over the Hub's existing durable turn ledger."""

import base64
import binascii
import hashlib
import json
import math
import re

from .hub_errors import HubAccessError
from .hub_store_helpers import (MAX_PROMPT_BYTES, bounded_text, identifier,
                                page_limit)
from .hub_turn_query import TURN_STATES, filters, read
from .memory_records import redact_content


def _stamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid turn timestamp")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError("invalid turn timestamp") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid turn timestamp")
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _binding(tenant, session, options=None, authority=""):
    if not options or (not authority and not options["q"] and options["state"] == "all"):
        return _digest([tenant, session])
    scope = _digest([tenant, session, options["q"], options["state"]])
    return authority or _digest([tenant]), scope


def _encode(binding, created_at, turn):
    scope = [1, binding] if isinstance(binding, str) else [2, *binding]
    raw = json.dumps([*scope, _stamp(created_at).hex(), turn], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_scope(payload, binding, legacy_binding):
    if not isinstance(payload, list) or not payload or type(payload[0]) is not int:
        raise ValueError("invalid turn cursor")
    if payload[0] == 1 and len(payload) == 4:
        expected = binding if isinstance(binding, str) else legacy_binding
        if expected is None or payload[1] != expected:
            raise ValueError("turn cursor does not match its filters")
        return payload[1], payload[2], payload[3]
    if payload[0] != 2 or len(payload) != 5 or isinstance(binding, str):
        raise ValueError("invalid turn cursor")
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in payload[1:3]):
        raise ValueError("invalid turn cursor binding")
    if payload[1] != binding[0]:
        raise HubAccessError("turn_cursor_authority_changed", "turn cursor authority changed", 403)
    if payload[2] != binding[1]:
        raise ValueError("turn cursor does not match its filters")
    return tuple(payload[1:3]), payload[3], payload[4]


def _decode(value, binding, legacy_binding=None):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid turn cursor")
    if not value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid turn cursor")
    try:
        payload = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        scope, stamp, turn = _cursor_scope(payload, binding, legacy_binding)
        if not isinstance(stamp, str):
            raise ValueError("invalid turn cursor")
        timestamp = _stamp(float.fromhex(stamp))
        identifier(turn, "turn cursor")
        if _encode(scope, timestamp, turn) != value:
            raise ValueError("noncanonical turn cursor")
        return timestamp, turn
    except (binascii.Error, OverflowError, TypeError, UnicodeError, RecursionError):
        raise ValueError("invalid turn cursor") from None


def _record(row):
    state = row["status"]
    if state not in TURN_STATES:
        raise ValueError("invalid recorded turn state")
    prompt = redact_content(bounded_text(row["prompt"], "prompt", MAX_PROMPT_BYTES, by_bytes=True))
    encoded = prompt.encode("utf-8")
    created, updated = _stamp(row["created_at"]), _stamp(row["updated_at"])
    if updated < created:
        raise ValueError("invalid recorded turn time order")
    return {"turn_id": identifier(row["turn_id"], "turn"),
            "session_id": identifier(row["session_id"], "session"),
            "instance_id": identifier(row["instance_id"], "instance"), "state": state,
            "created_at": created, "updated_at": updated,
            "prompt": encoded[:MAX_PROMPT_BYTES].decode("utf-8", errors="ignore"),
            "prompt_truncated": len(encoded) > MAX_PROMPT_BYTES}


def list_turns(hub, tenant, session_id, *, before="", limit=20, q="", state="all", turn_id="", authority=""):
    tenant, session_id = identifier(tenant, "tenant"), identifier(session_id, "session")
    limit = page_limit(limit, 50)
    options = filters(q, state, turn_id, before)
    if not isinstance(authority, str) or (authority and not re.fullmatch(r"[a-f0-9]{64}", authority)):
        raise ValueError("invalid turn cursor authority")
    binding = _binding(tenant, session_id, options, authority)
    legacy = _binding(tenant, session_id) if not options["q"] and state == "all" else None
    cursor = _decode(before, binding, legacy)
    limit = 1 if turn_id else limit
    rows = read(hub, tenant, session_id, options, cursor, limit, _record)
    selected = rows[:limit]
    next_cursor = ""
    if len(rows) > limit and not turn_id:
        last = selected[-1]
        next_cursor = _encode(binding, last["created_at"], last["turn_id"])
    return {"data": selected, "next_cursor": next_cursor}


def history_page(service, principal, session_id, query):
    """Validate the read boundary beside the durable history projection."""
    service._session(principal, session_id, "agent.sessions:read")
    if not isinstance(query, dict) or set(query) - {"limit", "before", "q", "state", "turn_id"}:
        raise ValueError("request contains unsupported fields")
    limit = query.get("limit", "20")
    if not isinstance(limit, str) or not re.fullmatch(r"[0-9]{1,2}", limit) or not 1 <= int(limit) <= 50:
        raise ValueError("turn history limit must be 1..50")
    if "turn_id" in query:
        identifier(query["turn_id"], "turn_id")
    authority = _digest([principal.tenant_id, principal.subject, principal.client_id, sorted(principal.instances)])
    return service.store.turn_history(principal.tenant_id, session_id, before=query.get("before", ""),
                                      limit=int(limit), q=query.get("q", ""), state=query.get("state", "all"),
                                      turn_id=query.get("turn_id", ""), authority=authority)


def is_path(path):
    from urllib.parse import unquote, unquote_to_bytes
    from .hub_service import PREFIX
    if not path.startswith(PREFIX + "/"):
        return False
    parts = [unquote(part) for part in path[len(PREFIX) + 1:].split("/")]
    if len(parts) != 3 or parts[0] != "sessions" or parts[2] != "turns":
        return False
    unquote_to_bytes(path).decode("utf-8")
    return True
