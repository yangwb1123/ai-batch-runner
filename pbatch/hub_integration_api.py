"""Explicit operator scopes for delivery diagnostics and bounded manual retries."""

from .hub_errors import HubAccessError


def dispatch(service, method, parts, query, body, principal):
    from .hub_service import _only, _page
    runtime = service.integrations
    if runtime is None:
        raise HubAccessError("integrations_unavailable", "integrations are not configured", 503)
    if method == "GET" and parts == ["integrations", "deliveries"]:
        principal.require("agent.integrations:read")
        _only(query, {"state", "after", "limit"})
        page = _page(query, events=True)
        rows = runtime.store.list(principal.tenant_id, principal.instance_filter,
                                  state=query.get("state", ""), **page)
        for row in rows:
            row["configured"] = (principal.tenant_id, row["sink_name"]) in runtime.sinks
        return 200, service._list(rows, "sequence", default=page["after"])
    if method == "POST" and len(parts) == 4 and parts[:2] == ["integrations", "deliveries"]:
        if parts[3] == "retry":
            _only(body, set())
            _only(query, set())
            principal.require("agent.integrations:retry")
            row = runtime.store.get(principal.tenant_id, parts[2])
            principal.require("agent.integrations:retry", row["instance_id"])
            retried = runtime.retry(principal.tenant_id, parts[2])
            return 202, {"data": {key: retried[key] for key in ("delivery_id", "state", "attempts")}}
    raise HubAccessError("not_found", "integration route not found", 404)
