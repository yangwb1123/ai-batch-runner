"""Workspace routes reuse compute scopes and active-turn authorization."""

from .hub_compute_models import fields
from .hub_errors import HubAccessError


def workspace_route(parts):
    return len(parts) == 3 and ((parts[0] == "sessions" and parts[2] == "snapshots")
                               or (parts[0] == "tasks" and parts[2] == "workspace"))


def dispatch(api, method, parts, query, body, principal, key):
    from .hub_service import _page
    workspace = api.compute.workspace
    if parts[0] == "tasks" and method == "GET":
        fields(query, set())
        task = api._task(principal, parts[1], "agent.tasks:read")
        return 200, {"data": workspace.download(principal.tenant_id, task)}
    if parts[0] != "sessions" or method not in {"GET", "POST"}:
        raise HubAccessError("method_not_allowed", "method not allowed", 405)
    scope = "agent.tasks:write" if method == "POST" else "agent.tasks:read"
    session = api.service._session(principal, parts[1], scope)
    if method == "GET":
        fields(query, {"after", "limit"})
        rows = workspace.store.list(principal.tenant_id, session["session_id"], **_page(query))
        return 200, api.service._list(rows, "snapshot_id")
    fields(query, set())
    fields(body, {"bundle"})
    return 200, {"data": workspace.upload(principal, session, body.get("bundle"), key)}


def delegated(api, operation, payload, principal, session_id, turn_id, key):
    if operation not in {"snapshots", "snapshot-upload", "workspace-output"}:
        return None
    workspace = api.compute.workspace
    scope = "agent.tasks:write" if operation == "snapshot-upload" else "agent.tasks:read"
    session = api.service._session(principal, session_id, scope)
    if operation == "snapshot-upload":
        fields(payload, {"bundle"})
        return workspace.upload(principal, session, payload.get("bundle"), key, turn_id=turn_id)
    if operation == "snapshots":
        fields(payload, {"after"})
        from .hub_service import _page
        page = _page({"after": payload.get("after", ""), "limit": "10"})
        return workspace.store.list(principal.tenant_id, session_id, **page)
    fields(payload, {"task_id"})
    task = api._task(principal, payload.get("task_id", ""), scope)
    if task["session_id"] != session_id or task["turn_id"] != turn_id or task["actor"] != principal.subject:
        raise HubAccessError("forbidden", "delegation is restricted to its active turn")
    return workspace.download(principal.tenant_id, task)
