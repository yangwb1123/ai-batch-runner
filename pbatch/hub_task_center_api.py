"""Authenticated read-only Hub task-center route. No new execution or data-upload scope."""
from __future__ import annotations

import re

from .hub_errors import HubAccessError
from .hub_task_center_store import read_metadata
from .hub_task_center_view import project

ROUTE = ["task-center"]


def is_route(parts, *, include_views=False):
    routes = (["task-center"], ["compute-task-center"], ["compute-task-center", "source-sessions"])
    return parts in routes or (include_views and parts in tuple(route + ["view"] for route in routes))


def options(limit: int, source: str = "") -> None:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("task limit must be between 1 and 200")
    if not isinstance(source, str) or (source and not re.fullmatch(r"src-[a-f0-9]{24}", source)):
        raise ValueError("Hub tasks need a complete source ID, not an instance name, path or URL")


def validate_read_headers(path: str, headers) -> None:
    from .hub_service import PREFIX
    if path.rstrip("/") not in {PREFIX + suffix for suffix in (
            "/task-center", "/task-center/view", "/compute-task-center", "/compute-task-center/view",
            "/compute-task-center/source-sessions", "/compute-task-center/source-sessions/view")}:
        return
    if (len(headers.get_all("Content-Length", [])) > 1 or headers.get("Content-Length", "0") != "0" or
            any(headers.get_all(name, []) for name in ("Transfer-Encoding", "Idempotency-Key", "If-Match"))):
        raise ValueError("Hub task-center GET does not accept a body or mutation headers")


def dispatch(service, method: str, query: dict, principal, *, compute=False, source_sessions=False) -> tuple[int, dict]:
    if method != "GET":
        raise HubAccessError("method_not_allowed", "task-center is read-only", 405)
    principal.require("agent.instances:read")
    principal.require("agent.tasks:read" if compute else "agent.sessions:read")
    if source_sessions:
        if not compute:
            raise ValueError("source-session references require explicit compute observation")
        principal.require("agent.sessions:read")
    if set(query) - {"source", "limit"}:
        raise ValueError("unsupported task-center query")
    value = query.get("limit", "100")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,3}", value):
        raise ValueError("invalid task limit")
    limit, source = int(value), query.get("source", "")
    options(limit, source)
    if compute and service.compute_api.compute is None:
        raise HubAccessError("compute_unavailable", "compute is not configured", 503)
    reader, projector = read_metadata, project
    if compute:
        from .hub_compute_task_center import read_metadata as reader, project as projector
    if source_sessions:
        from .hub_task_source_sessions import read_metadata as reader, project as projector
    raw = reader(service.store, principal.tenant_id, principal.instance_filter, source)
    return 200, {"data": projector(principal.tenant_id, raw, limit, source)}
