"""Idle session close routes retain the existing tenant and instance grants."""

from .hub_errors import HubAccessError


def is_route(parts):
    return ((len(parts) == 3 and parts[0] == "sessions" and parts[2] == "close")
            or (len(parts) == 2 and parts[0] == "session-close-requests"))


def dispatch(service, method, parts, query, body, principal, key):
    from .hub_service import _only
    _only(query, set())
    _only(body, set())
    if parts[0] == "sessions" and method == "POST":
        service._session(principal, parts[1], "agent.sessions:write")
        result = service.store.close_session(
            principal.tenant_id, parts[1], principal.subject, key, client_id=principal.client_id)
        return 202, {"data": result}
    if parts[0] == "session-close-requests" and method == "GET":
        principal.require("agent.sessions:read")
        result = service.store.session_close_request(principal.tenant_id, parts[1])
        principal.require("agent.sessions:read", result["instance_id"])
        return 200, {"data": result}
    raise HubAccessError("method_not_allowed", "method not allowed", 405)


def gateway(service, method, query, body, principal, instance):
    from .hub_service import _only
    values = query if method == "GET" else body
    if method == "GET":
        _only(body, set())
        _only(query, {"instance_id", "generation"})
        result = service.store.session_close_requests(
            principal.tenant_id, instance, service._generation(values))
    elif method == "POST":
        _only(query, set())
        _only(body, {"instance_id", "generation", "request_id", "status", "error_code"})
        result = service.store.report_session_close_request(
            principal.tenant_id, instance, service._generation(values), body)
    else:
        raise HubAccessError("method_not_allowed", "method not allowed", 405)
    return 200, {"data": result}
