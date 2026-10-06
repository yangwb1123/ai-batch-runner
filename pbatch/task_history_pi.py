"""Pi v3 JSONL tree: copy textual user messages from one explicitly chosen leaf."""
from __future__ import annotations

from .task_goal_schema import text
from .task_intent_schema import fingerprint
from .task_native_history import metadata, user_text, workspace


def _tree(rows: list) -> dict:
    nodes = {}
    for row in rows:
        key, parent = row.get("id"), row.get("parentId")
        if row.get("type") == "session" or not text(key, 128) or "parentId" not in row:
            raise ValueError("unsupported Pi entry identity/parent shape")
        if key in nodes or (parent is not None and (not text(parent, 128) or parent not in nodes)):
            raise ValueError("duplicate, dangling or reordered Pi history ancestry")
        nodes[key] = row
    return nodes


def _branch(nodes: dict, leaf: str) -> list:
    if leaf not in nodes:
        raise ValueError("explicit Pi leaf was not found in the selected file")
    selected = []
    while leaf is not None:
        row = nodes[leaf]
        selected.append(row)
        leaf = row["parentId"]  # parents precede children; cycles cannot pass _tree
    return list(reversed(selected))


def project(rows: list, source: dict, leaf: str) -> tuple[list, dict]:
    header = rows[0]
    if header.get("type") != "session" or type(header.get("version")) is not int or header["version"] != 3:
        raise ValueError("only an explicit Pi v3 session header is supported")
    identity = header.get("id")
    if not text(identity, 128):
        raise ValueError("Pi native session identity is missing")
    root = workspace(source, header.get("cwd"))
    nodes = _tree(rows[1:])
    selected, messages = _branch(nodes, leaf), []
    all_users = 0
    for row in nodes.values():
        if row["type"] == "message":
            message = row.get("message")
            if not isinstance(message, dict) or not text(message.get("role"), 80):
                raise ValueError("Pi message role is missing")
            all_users += message["role"] == "user"
    for row in selected:
        if row["type"] != "message" or row["message"]["role"] != "user":
            continue
        messages.append({"id": "pi-" + fingerprint([identity, row["id"]]),
                         "ts": row.get("timestamp"), "role": "user",
                         "text": user_text(row["message"].get("content"))})
    return messages, metadata("pi-session", identity, root, leaf, rows, messages, all_users)
