"""Append-order isolate session metadata, with no guessed identity or liveness."""
from __future__ import annotations

import re

from .session_merge import SESSION_STATES

UNFINISHED_SESSION_STATES = frozenset(
    {"CREATED", "RUNNING", "REBASING", "VALIDATING", "MERGING"})
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _record(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    key = value.get("session_id")
    state = value.get("status", value.get("state"))
    if not isinstance(key, str) or not _SESSION_ID.fullmatch(key):
        return None
    if not isinstance(state, str) or state not in SESSION_STATES:
        return None
    if "state" in value and value["state"] != state:
        return None
    return key, state


def session_facts(records: list, window: dict) -> dict:
    """Latest *per full session ID*, not one arbitrary session per workspace.

    A partial window is not the complete set of sessions. A damaged/incomplete
    suffix cannot be bypassed to publish an older state as current metadata.
    """
    latest = {}
    last = None
    for value in records:
        item = _record(value)
        if item is None:
            if value is not None:
                window["malformed"] += 1
            last = None
            continue
        key, state = item
        latest[key] = state
        last = state
    pending = [{"session_id": key, "state": state}
               for key, state in sorted(latest.items())
               if state in UNFINISHED_SESSION_STATES]
    uncertain = bool(window["malformed"] or window["incomplete"] or window["error"])
    return {"session_state": None if uncertain else last,
            "session_unfinished": None if uncertain else bool(pending),
            "sessions_observed": len(latest), "unfinished_sessions": pending}
