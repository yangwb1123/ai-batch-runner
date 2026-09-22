"""Read-only operation history shares the existing session and instance grants."""

from .hub_errors import HubAccessError
from .hub_store_helpers import identifier


def is_route(parts):
    return parts == ["session-operations"]


def dispatch(service, method, query, body, principal):
    from .hub_service import _only
    if method != "GET":
        raise HubAccessError("method_not_allowed", "method not allowed", 405)
    _only(body, set())
    _only(query, {"instance_id", "before", "limit"})
    principal.require("agent.sessions:read")
    instance = identifier(query["instance_id"], "instance_id") if "instance_id" in query else ""
    if instance:
        principal.require("agent.sessions:read", instance)
    limit = query.get("limit", "20")
    if not isinstance(limit, str) or not limit.isascii() or not limit.isdecimal() or len(limit.lstrip("0")) > 2:
        raise ValueError("operation limit must be a decimal integer between 1 and 50")
    result = service.store.session_operations(
        principal.tenant_id, instance, before=query.get("before", ""), limit=int(limit.lstrip("0") or "0"),
        allowed_instances=principal.instance_filter)
    return 200, result
