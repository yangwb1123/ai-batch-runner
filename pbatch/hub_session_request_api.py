"""Session creation routes use existing tenant and exact-instance grants."""

from .hub_errors import HubAccessError


def is_route(parts):
    return ((len(parts) == 3 and parts[0] == "instances" and parts[2] == "sessions")
            or (len(parts) == 2 and parts[0] == "session-requests"))


def validate_read_headers(path, headers):
    from urllib.parse import unquote
    from .hub_service import PREFIX
    if not path.startswith(PREFIX + "/"):
        return
    parts = [unquote(part) for part in path[len(PREFIX) + 1:].split("/")]
    names = {"session-requests", "session-close-requests"}
    if not (parts == ["session-operations"] or (len(parts) == 2 and parts[0] in names)
            or (len(parts) == 2 and parts[0] == "gateway" and parts[1] in names)):
        return
    lengths = headers.get_all("Content-Length", [])
    if headers.get("Transfer-Encoding") or len(lengths) > 1 or any(value != "0" for value in lengths):
        raise ValueError("session request reads do not accept a body")


def dispatch(service, method, parts, query, body, principal, key):
    from .hub_service import _only
    _only(query, set())
    if parts[0] == "instances" and method == "POST":
        principal.require("agent.sessions:write", parts[1])
        _only(body, {"name"})
        result = service.store.create_session(
            principal.tenant_id, parts[1], principal.subject, body.get("name"), key,
            client_id=principal.client_id)
        return 202, {"data": result}
    if parts[0] == "session-requests" and method == "GET":
        _only(body, set())
        principal.require("agent.sessions:read")
        result = service.store.session_request(principal.tenant_id, parts[1])
        principal.require("agent.sessions:read", result["instance_id"])
        return 200, {"data": result}
    raise HubAccessError("method_not_allowed", "method not allowed", 405)


def gateway(service, method, query, body, principal, instance):
    from .hub_service import _only
    values = query if method == "GET" else body
    if method == "GET":
        _only(body, set())
        _only(query, {"instance_id", "generation"})
        result = service.store.session_requests(
            principal.tenant_id, instance, service._generation(values))
    elif method == "POST":
        _only(query, set())
        _only(body, {"instance_id", "generation", "request_id", "status", "error_code"})
        result = service.store.report_session_request(
            principal.tenant_id, instance, service._generation(values), body)
    else:
        raise HubAccessError("method_not_allowed", "method not allowed", 405)
    return 200, {"data": result}
