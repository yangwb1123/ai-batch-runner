"""Transport-independent Agent Hub routing and tenant/resource authorization."""

from __future__ import annotations

from urllib.parse import unquote

from .hub_auth import GATEWAY_SCOPE, HubPrincipal
from .hub_errors import HubAccessError


PREFIX = "/api/v1/agent"
_READ = "agent.sessions:read"


def _only(body: dict, fields: set[str]) -> dict:
    if not isinstance(body, dict) or set(body) - fields:
        raise ValueError("request contains unsupported fields")
    return body


def _page(query: dict, *, events: bool = False) -> dict:
    limit = query.get("limit", "100")
    if not isinstance(limit, str) or not limit.isdigit() or not 1 <= int(limit) <= 200:
        raise ValueError("limit must be between 1 and 200")
    after = query.get("after", "0" if events else "")
    if events:
        if not str(after).isdigit() or len(str(after)) > 18:
            raise ValueError("event cursor must be a nonnegative integer")
        return {"limit": int(limit), "after": int(after)}
    if len(after) > 256:
        raise ValueError("invalid page cursor")
    return {"limit": int(limit), "after": after}


class HubService:
    def __init__(self, store, compute=None, *, integrations=None, identity=None):
        from .hub_compute_api import ComputeAPI
        self.store = store
        self.compute_api = ComputeAPI(self, compute)
        self.integrations, self.identity = integrations, identity

    def dispatch(self, method: str, path: str, query: dict, body: dict,
                 principal: HubPrincipal, idempotency_key: str = "") -> tuple[int, dict]:
        if not path.startswith(PREFIX + "/"):
            raise HubAccessError("not_found", "route not found", 404)
        parts = [unquote(part) for part in path[len(PREFIX) + 1:].split("/")]
        if any(not part or "/" in part or "\\" in part for part in parts):
            raise ValueError("invalid route")
        if parts[0] == "gateway":
            return self._gateway(method, parts[1:], query, body, principal)
        if principal.kind != "user":
            raise HubAccessError("forbidden", "user access required")
        if self.identity is not None:
            self.identity.check(principal)
        from .hub_session_operations_api import is_route as operations_route, dispatch as operations_dispatch
        if operations_route(parts):
            return operations_dispatch(self, method, query, body, principal)
        from .hub_session_close_api import is_route as is_close_route, dispatch as close_dispatch
        if is_close_route(parts):
            return close_dispatch(self, method, parts, query, body, principal, idempotency_key)
        from .hub_session_request_api import is_route, dispatch
        if is_route(parts):
            return dispatch(self, method, parts, query, body, principal, idempotency_key)
        if parts[0] == "integrations":
            from .hub_integration_api import dispatch
            return dispatch(self, method, parts, query, body, principal)
        from .hub_compute_api import compute_route
        if compute_route(parts):
            return self.compute_api.dispatch(method, parts, query, body, principal, idempotency_key)
        if method == "GET":
            return 200, self._get(parts, query, principal)
        if method == "POST":
            return 202, {"data": self._post(parts, body, principal, idempotency_key)}
        raise HubAccessError("method_not_allowed", "method not allowed", 405)

    def _session(self, principal: HubPrincipal, session_id: str, scope: str) -> dict:
        principal.require(scope)
        session = self.store.session(principal.tenant_id, session_id)
        principal.require(scope, session["instance_id"])
        return session

    def _turn(self, principal: HubPrincipal, turn_id: str, scope: str) -> dict:
        principal.require(scope)
        turn = self.store.turn(principal.tenant_id, turn_id)
        self._session(principal, turn["session_id"], scope)
        return turn

    def _get(self, parts: list, query: dict, principal: HubPrincipal) -> dict:
        if parts in (["task-center"], ["compute-task-center"], ["compute-task-center", "source-sessions"]):
            from .hub_task_center_api import dispatch
            return dispatch(self, "GET", query, principal, compute=parts[0] == "compute-task-center",
                            source_sessions=parts == ["compute-task-center", "source-sessions"])[1]
        if set(query) - {"instance_id", "after", "limit"}:
            raise ValueError("unsupported query parameter")
        if parts == ["instances"]:
            principal.require("agent.instances:read")
            rows = self.store.instances(principal.tenant_id, **_page(query),
                                        allowed_instances=principal.instance_filter)
            return self._list(rows, "instance_id")
        if parts == ["sessions"]:
            principal.require(_READ)
            instance = query.get("instance_id", "")
            if instance:
                principal.require(_READ, instance)
            rows = self.store.sessions(principal.tenant_id, instance, **_page(query),
                                       allowed_instances=principal.instance_filter)
            return self._list(rows, "session_id")
        if len(parts) >= 2 and parts[0] == "sessions":
            return self._session_get(parts, query, principal)
        if len(parts) == 2 and parts[0] == "turns":
            return {"data": self._turn(principal, parts[1], _READ)}
        raise HubAccessError("not_found", "route not found", 404)

    def _session_get(self, parts: list, query: dict, principal: HubPrincipal) -> dict:
        session = self._session(principal, parts[1], _READ)
        if len(parts) == 2:
            return {"data": session}
        if len(parts) == 3 and parts[2] == "events":
            rows = self.store.events(principal.tenant_id, parts[1], **_page(query, events=True))
            return self._list(rows, "cursor", default=int(query.get("after", "0")))
        raise HubAccessError("not_found", "route not found", 404)

    @staticmethod
    def _list(rows: list, cursor_key: str, default="") -> dict:
        return {"data": rows, "next_cursor": rows[-1][cursor_key] if rows else default}

    def _post(self, parts: list, body: dict, principal: HubPrincipal, key: str) -> dict:
        from .hub_task_center_api import is_route
        if is_route(parts, include_views=True):
            raise HubAccessError("method_not_allowed", "task-center is read-only", 405)
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "turns":
            self._session(principal, parts[1], "agent.sessions:write")
            _only(body, {"prompt"})
            from .hub_compute_models import snapshot
            return self.store.submit_turn(principal.tenant_id, parts[1], principal.subject,
                                          body.get("prompt"), key, authority=snapshot(principal))
        if len(parts) == 3 and parts[0] == "turns" and parts[2] == "cancel":
            self._turn(principal, parts[1], "agent.turns:cancel")
            _only(body, set())
            return self.store.cancel_turn(principal.tenant_id, parts[1], principal.subject)
        raise HubAccessError("not_found", "route not found", 404)

    def _gateway(self, method: str, parts: list, query: dict,
                 body: dict, principal: HubPrincipal) -> tuple[int, dict]:
        if principal.kind != "gateway":
            raise HubAccessError("forbidden", "gateway access required")
        principal.require(GATEWAY_SCOPE)
        if parts == ["session-close-requests"] and method not in {"GET", "POST"}:
            raise HubAccessError("method_not_allowed", "method not allowed", 405)
        values = query if method == "GET" else body
        instance = values.get("instance_id", "")
        if not isinstance(instance, str) or not instance:
            raise ValueError("instance_id is required")
        principal.require(GATEWAY_SCOPE, instance)
        if parts == ["session-close-requests"]:
            from .hub_session_close_api import gateway
            return gateway(self, method, query, body, principal, instance)
        if parts == ["session-requests"]:
            from .hub_session_request_api import gateway
            return gateway(self, method, query, body, principal, instance)
        if method == "GET" and parts == ["commands"]:
            _only(values, {"instance_id", "generation"})
            generation = self._generation(values)
            return 200, {"data": self.store.commands(principal.tenant_id, instance, generation)}
        if method != "POST" or len(parts) != 1:
            raise HubAccessError("not_found", "route not found", 404)
        if parts == ["compute"]:
            if self.compute_api.compute is None:
                raise HubAccessError("compute_unavailable", "compute is not configured", 503)
            return 200, {"data": self.compute_api.delegated(body, principal)}
        result = self._gateway_write(parts[0], principal.tenant_id, instance, values)
        return 200, {"data": result}

    @staticmethod
    def _generation(values: dict) -> int:
        value = values.get("generation")
        if isinstance(value, bool) or not str(value).isdigit() or len(str(value)) > 10:
            raise ValueError("generation must be a positive integer")
        if int(value) < 1:
            raise ValueError("generation must be a positive integer")
        return int(value)

    def _gateway_write(self, route: str, tenant: str, instance: str, body: dict) -> dict:
        common = {"instance_id", "generation"}
        if route == "register":
            _only(body, {"instance_id", "boot_id", "name", "project_id", "session_capacity",
                         "session_close_supported"})
            return self.store.register_instance(tenant, instance, body)
        generation = self._generation(body)
        if route == "heartbeat":
            _only(body, common)
            return self.store.heartbeat(tenant, instance, generation)
        if route == "sessions":
            _only(body, common | {"local_session_id", "name", "project_id", "status", "controllable"})
            return self.store.upsert_session(tenant, instance, generation, body)
        if route == "events":
            _only(body, common | {"event_id", "session_id", "turn_id", "kind", "payload"})
            if body.get("kind") not in {"turn.running", "turn.completed", "turn.failed",
                                         "turn.interrupted", "message.delta", "message.completed"}:
                raise ValueError("unsupported Gateway event kind")
            from .memory_records import redact_content
            body = dict(body, payload=redact_content(body.get("payload", {})))
            return self.store.append_event(tenant, instance, generation, body)
        raise HubAccessError("not_found", "route not found", 404)
