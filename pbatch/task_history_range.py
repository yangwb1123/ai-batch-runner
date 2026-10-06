"""Explicit native user ranges with a compact Pi ancestry index and bounded two-pass reads."""
from __future__ import annotations

from . import task_history_pi as pi, task_history_codex as codex
from .task_goal_schema import text
from .task_intent_schema import MAX_MESSAGES, fingerprint, validate_messages
from .task_history_stream import MAX_SCAN_RECORDS, Snapshot, authorized_path


def validate_range(first: int, last: int) -> None:
    if type(first) is not int or type(last) is not int or not 1 <= first <= last:
        raise ValueError("explicit positive first/last user ordinals are required")
    if last >= MAX_SCAN_RECORDS:
        raise ValueError("explicit user ordinal exceeds the native record budget")
    if last - first + 1 > MAX_MESSAGES:
        raise ValueError("selected user range exceeds the message budget; no messages were truncated")


def _is_user(row: dict, format: str) -> bool:
    if format == "pi-session":
        if row["type"] != "message":
            return False
        payload = row.get("message")
        if not isinstance(payload, dict) or not text(payload.get("role"), 80):
            raise ValueError("Pi message role is missing")
        return payload["role"] == "user"
    if row["type"] == "session_meta":
        raise ValueError("multiple Codex sessions cannot share an export")
    if row["type"] != "event_msg":
        return False
    payload = row.get("payload")
    if not isinstance(payload, dict) or not text(payload.get("type"), 80):
        raise ValueError("Codex event message type is missing")
    return payload["type"] == "user_message"


def _index(snapshot: Snapshot, source: dict, format: str) -> tuple:
    nodes, users, identity, root = {}, [], "", ""
    adapter = pi if format == "pi-session" else codex
    for ordinal, row in snapshot.rows():
        if ordinal == 0:
            identity, root = adapter.header_identity(row, source)
            continue
        user = _is_user(row, format)
        if format == "pi-session":
            key, parent = pi.entry_identity(row, nodes)
            nodes[key] = (parent, ordinal, user)  # never retain tool/assistant bodies
        if user:
            users.append(ordinal)
    return nodes, users, identity, root


def _branch_users(nodes: dict, leaf: str) -> list:
    if leaf not in nodes:
        raise ValueError("explicit Pi leaf was not found in the selected file")
    users = []
    while leaf is not None:
        parent, ordinal, user = nodes[leaf]
        if user:
            users.append(ordinal)
        leaf = parent
    return list(reversed(users))


def _collect(snapshot: Snapshot, format: str, identity: str, selected: set) -> list:
    messages = []
    for ordinal, row in snapshot.rows():
        if ordinal in selected:
            messages.append(pi.user_record(row, identity) if format == "pi-session"
                            else codex.user_record(row, identity, ordinal))
    validate_messages(messages)
    return messages


def scan(source: dict, file: str, format: str, leaf: str, first: int, last: int,
         *, upto: int | None = None, prefixes: list | None = None) -> tuple[list, dict, dict]:
    from .task_native_history import options
    options(format, "range_scan", leaf)
    if format == "normalized":
        raise ValueError("range export requires a supported native format")
    validate_range(first, last)
    path = authorized_path(source, file)
    initial = Snapshot(path, upto, prefixes)
    nodes, users, identity, root = _index(initial, source, format)
    branch = _branch_users(nodes, leaf) if format == "pi-session" else users
    if last > len(branch):
        raise ValueError("explicit user range is outside the selected native branch/file")
    selected = set(branch[first - 1:last])
    replay = Snapshot(path, initial.total)
    messages = _collect(replay, format, identity, selected)
    if replay.proof != initial.proof or replay.stat_key != initial.stat_key:
        raise ValueError("native history changed between bounded export passes")
    report = {"format": format, "session_digest": fingerprint([format, identity]),
        "workspace_digest": root, "leaf_id": leaf, "input_evidence": initial.proof,
        "selection": {"first_user": first, "last_user": last, "branch_user_messages": len(branch),
                      "native_user_messages": len(users), "native_records": initial.records}}
    return messages, report, initial.prefixes
