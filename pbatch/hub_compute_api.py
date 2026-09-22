"""Compute routes and active-turn delegated authorization."""

from __future__ import annotations

import json
from dataclasses import replace

from .hub_compute_models import fields, request_body, reschedule_target, retry_target
from .hub_errors import HubAccessError
from . import hub_workspace_api
from . import hub_compute_placement
from .hub_store_helpers import (check_session_owner, get_session, get_turn_for_session,
                                require_live_generation)


def compute_route(parts: list) -> bool:
    return hub_workspace_api.workspace_route(parts) or bool(parts and (parts[0] in {"devices", "tasks"} or
                           len(parts) == 3 and parts[0] == "sessions" and parts[2] == "tasks"))


class ComputeAPI:
    def __init__(self, service, compute):
        self.service = service
        self.compute = compute

    def dispatch(self, method, parts, query, body, principal, key):
        if hub_compute_placement.is_route(parts):
            if method != "GET":
                raise HubAccessError("method_not_allowed", "method not allowed", 405)
            fields(body, set())
        if self.compute is None:
            raise HubAccessError("compute_unavailable", "compute is not configured", 503)
        if hub_workspace_api.workspace_route(parts):
            return hub_workspace_api.dispatch(self, method, parts, query, body, principal, key)
        if method == "GET":
            return 200, self._get(parts, query, principal)
        if method == "POST":
            return 202, {"data": self._post(parts, body, principal, key)}
        raise HubAccessError("method_not_allowed", "method not allowed", 405)

    def _get(self, parts, query, principal):
        from .hub_service import _page
        if hub_compute_placement.is_route(parts):
            return {"data": hub_compute_placement.read_placement(
                self.compute, principal, parts[1], **hub_compute_placement.page_query(query))}
        if parts == ["devices"]:
            fields(query, {"project_id", "after", "limit"})
            principal.require("agent.devices:read")
            page = _page(query)
            rows = self._device_page(query.get("project_id", ""), page, principal)
            return self.service._list(rows, "device_id")
        fields(query, {"session_id", "after", "limit"})
        principal.require("agent.tasks:read")
        if parts == ["tasks"]:
            session = query.get("session_id", "")
            if session:
                self.service._session(principal, session, "agent.tasks:read")
            rows = self.compute.store.list(principal.tenant_id, principal.instance_filter,
                                            session_id=session, **_page(query))
            return self.service._list(rows, "task_id")
        if len(parts) == 2 and parts[0] == "tasks":
            return {"data": self._task(principal, parts[1], "agent.tasks:read")}
        raise HubAccessError("not_found", "compute route not found", 404)

    def _device_page(self, project, page, principal):
        bindings = self.compute.bindings(principal.tenant_id)
        targets = sorted(device for device, binding in bindings.items()
                         if principal.allows_compute(binding["instance_id"])
                         and (not project or project in binding["workspaces"])
                         and device > page["after"])
        batch_size = min(page["limit"], 16)
        result, size = [], 2
        for offset in range(0, len(targets), batch_size):
            rows = self.compute.inventory(principal.tenant_id, target_ids=targets[offset:offset + batch_size])
            for row in sorted(rows, key=lambda item: item["device_id"]):
                row_size = len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode()) + 2
                if size + row_size > 6 * 1024**2 or len(result) == page["limit"]:
                    if not result:
                        raise HubAccessError("device_inventory_too_large", "device inventory exceeds page limit", 503)
                    return result
                result.append(row)
                size += row_size
            if len(result) == page["limit"]:
                break
        return result

    def _task(self, principal, task_id, scope):
        principal.require(scope)
        task = self.compute.store.get(principal.tenant_id, task_id)
        principal.require(scope, task["instance_id"])
        return task

    def _post(self, parts, body, principal, key):
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
            return self._cancel_post(parts[1], body, principal)
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "reschedule":
            return self._reschedule_post(parts[1], body, principal, key)
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "retry":
            return self._retry_post(parts[1], body, principal, key)
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "tasks":
            return self._submit_post(parts[1], body, principal, key)
        raise HubAccessError("not_found", "compute route not found", 404)

    def _cancel_post(self, task_id, body, principal):
        fields(body, set())
        self._task(principal, task_id, "agent.tasks:cancel")
        task = self.compute.store.cancel(principal.tenant_id, task_id, actor=principal.subject)
        return mutation_response(task, principal)

    def _reschedule_post(self, task_id, body, principal, key):
        if key:
            raise ValueError("reschedule does not accept an idempotency key")
        target = reschedule_target(body)
        principal.require("agent.tasks:write")
        task = self.compute.store.get(principal.tenant_id, task_id, private=True)
        principal.require("agent.tasks:write", task["instance_id"])
        if task["actor"] != principal.subject:
            raise HubAccessError("forbidden", "only the task actor may reschedule it")
        bindings = self.compute.bindings(principal.tenant_id)
        allowed = {device for device, binding in bindings.items()
                   if device in task["_targets"]
                   and principal.allows_compute(binding["instance_id"])
                   and task["project_id"] in binding["workspaces"]}
        if not allowed:
            raise HubAccessError("forbidden", "no authorized project device matches the task")
        if target and target not in allowed:
            raise HubAccessError("forbidden", "target device is not authorized for this task")
        result = self.compute.store.reschedule(
            principal.tenant_id, task_id, target, actor=principal.subject)
        return mutation_response(result, principal)

    def _retry_post(self, task_id, body, principal, key):
        if key:
            raise ValueError("retry does not accept an idempotency key")
        requested = retry_target(body)
        principal.require("agent.tasks:write")
        task = self.compute.store.get(principal.tenant_id, task_id, private=True)
        principal.require("agent.tasks:write", task["instance_id"])
        if task["actor"] != principal.subject:
            raise HubAccessError("forbidden", "only the task actor may retry it")
        bindings = self.compute.bindings(principal.tenant_id)
        allowed = {device for device, binding in bindings.items()
                   if device in task["_targets"]
                   and principal.allows_compute(binding["instance_id"])
                   and task["project_id"] in binding["workspaces"]}
        if not allowed:
            raise HubAccessError("forbidden", "no authorized project device matches the task")
        # An omitted target preserves an original pin.  Only an originally
        # automatic task is allowed to become automatic again.
        target = requested or task.get("target_device_id", "")
        if target and target not in allowed:
            raise HubAccessError("forbidden", "target device is not authorized for this task")
        result = self.compute.store.retry_lost(
            principal.tenant_id, task_id, target, actor=principal.subject,
            confirm_duplicate=True)
        return mutation_response(result, principal)

    def _submit_post(self, session_id, body, principal, key):
        session = self.service._session(principal, session_id, "agent.tasks:write")
        payload = request_body(body)
        if payload["turn_id"]:
            turn = self.service.store.turn(principal.tenant_id, payload["turn_id"])
            if turn["session_id"] != session["session_id"] or turn["actor"] != principal.subject:
                raise HubAccessError("forbidden", "turn must belong to this actor and session")
        bindings = self.compute.bindings(principal.tenant_id)
        targets = [device for device, binding in bindings.items()
                   if principal.allows_compute(binding["instance_id"])
                   and session["project_id"] in binding["workspaces"]]
        if "workspace" in payload:
            targets = [device for device in targets
                       if self.compute.connection_for_device(principal.tenant_id, device).archive is not None]
        if not targets or payload["target_device_id"] and payload["target_device_id"] not in targets:
            raise HubAccessError("forbidden", "no authorized project device matches the request")
        task = self.compute.store.submit(principal, session, payload, key, targets,
                                         self.compute.archive_enabled(principal.tenant_id),
                                         workspace_input=self.compute.workspace.bind(
                                             principal.tenant_id, session, payload,
                                             payload["target_device_id"]))
        return mutation_response(task, principal)

    def delegated(self, body, gateway):
        fields(body, {"instance_id", "generation", "session_id", "turn_id", "operation", "payload", "key"})
        session_id, turn_id = body.get("session_id", ""), body.get("turn_id", "")
        generation = self.service._generation(body)
        conn = self.service.store._connect()
        try:
            user = delegated_principal(conn, self.service.store, gateway, session_id, turn_id, generation)
        finally:
            conn.close()
        if self.service.identity is not None:
            self.service.identity.check_actor(user.tenant_id, user.subject, user.client_id)
        payload = body.get("payload", {})
        fields(payload, set(payload) if isinstance(payload, dict) else set())
        operation = body.get("operation")
        if operation != "snapshot-upload" and len(json.dumps(body, ensure_ascii=False).encode()) > 65536:
            raise HubAccessError("too_large", "compute operation exceeds 64 KiB", 413)
        workspace = hub_workspace_api.delegated(self, operation, payload, user, session_id,
                                                 turn_id, body.get("key", ""))
        if workspace is not None:
            return workspace
        if operation == "devices":
            return self._delegated_devices(payload, user, session_id)
        if operation == "submit":
            return self._post(["sessions", session_id, "tasks"], dict(payload, turn_id=turn_id),
                              user, body.get("key", ""))
        if operation == "placement":
            return self._delegated_placement(payload, user, session_id, turn_id, body.get("key", ""))
        if operation == "reschedule":
            return self._delegated_reschedule(payload, user, session_id, turn_id, body.get("key", ""))
        if operation == "retry":
            return self._delegated_retry(payload, user, session_id, turn_id, body.get("key", ""))
        fields(payload, {"task_id"})
        scope = "agent.tasks:cancel" if operation == "cancel" else "agent.tasks:read"
        task = self._task(user, payload.get("task_id", ""), scope)
        if (task["session_id"] != session_id or task["turn_id"] != turn_id
                or task["actor"] != user.subject):
            raise HubAccessError("forbidden", "delegation is restricted to its active turn")
        if operation == "status":
            return task
        if operation == "cancel":
            return self._post(["tasks", task["task_id"], "cancel"], {}, user, "")
        raise ValueError("unsupported delegated operation")

    def _delegated_reschedule(self, payload, user, session_id, turn_id, key):
        fields(payload, {"task_id", "target_device_id"})
        if "target_device_id" not in payload:
            raise ValueError("target_device_id is required")
        if key:
            raise ValueError("reschedule does not accept an idempotency key")
        task = self._task(user, payload.get("task_id", ""), "agent.tasks:write")
        if (task["session_id"] != session_id or task["turn_id"] != turn_id
                or task["actor"] != user.subject):
            raise HubAccessError("forbidden", "delegation is restricted to its active turn")
        return self._post(
            ["tasks", task["task_id"], "reschedule"],
            {"target_device_id": payload["target_device_id"]}, user, "")

    def _delegated_retry(self, payload, user, session_id, turn_id, key):
        fields(payload, {"task_id", "target_device_id", "confirm_duplicate"})
        if key:
            raise ValueError("retry does not accept an idempotency key")
        task = self._task(user, payload.get("task_id", ""), "agent.tasks:write")
        if (task["session_id"] != session_id or task["turn_id"] != turn_id
                or task["actor"] != user.subject):
            raise HubAccessError("forbidden", "delegation is restricted to its active turn")
        return self._post(
            ["tasks", task["task_id"], "retry"],
            {"target_device_id": payload.get("target_device_id", ""),
             "confirm_duplicate": payload.get("confirm_duplicate")}, user, "")

    def _delegated_placement(self, payload, user, session_id, turn_id, key):
        fields(payload, {"task_id", "after", "limit"})
        if key != "":
            raise ValueError("placement diagnostics do not accept an idempotency key")
        page = hub_compute_placement.page_values(payload.get("after", ""), payload.get("limit", 20))
        task = self._task(user, payload.get("task_id", ""), "agent.tasks:read")
        if (task["session_id"] != session_id or task["turn_id"] != turn_id
                or task["actor"] != user.subject):
            raise HubAccessError("forbidden", "delegation is restricted to its active turn")
        return hub_compute_placement.read_placement(self.compute, user, task["task_id"], **page)

    def _delegated_devices(self, payload, user, session_id):
        fields(payload, {"after"})
        session = self.service.store.session(user.tenant_id, session_id)
        query = {"project_id": session["project_id"], "after": payload.get("after", ""), "limit": "4"}
        rows = self._get(["devices"], query, user)["data"]
        page = []
        for row in rows:
            candidate = dict(row, project_ids=[session["project_id"]])
            if len(json.dumps(page + [candidate], ensure_ascii=False, allow_nan=False).encode()) > 60 * 1024:
                if not page:
                    raise HubAccessError("device_inventory_too_large", "device inventory exceeds tool limit", 503)
                break
            page.append(candidate)
        return page


def delegated_principal(conn, hub, gateway, session_id, turn_id, generation):
    from .hub_auth import GATEWAY_SCOPE
    instance = next(iter(gateway.instances))
    gateway.require(GATEWAY_SCOPE, instance)
    require_live_generation(conn, gateway.tenant_id, instance, generation, hub.clock(), hub.offline_after)
    session = get_session(conn, gateway.tenant_id, session_id)
    check_session_owner(session, instance, generation)
    turn = get_turn_for_session(conn, gateway.tenant_id, turn_id, session_id)
    if turn["status"] != "running" or turn["generation"] != generation:
        raise HubAccessError("delegation_expired", "compute delegation requires an active running turn")
    row = conn.execute("SELECT authority_json FROM hub_compute_authority WHERE turn_id=?", (turn_id,)).fetchone()
    authority = json.loads(row[0]) if row else {}
    permitted = frozenset(authority.get("instances", []))
    targets = permitted if "*" in gateway.compute_targets else gateway.compute_targets
    if "*" not in permitted:
        targets &= permitted
    return replace(gateway, kind="delegated", subject=turn["actor"],
                   client_id=authority.get("client_id", ""),
                   scopes=gateway.scopes & frozenset(authority.get("scopes", [])),
                   compute_instances=targets)


def mutation_response(task: dict, principal) -> dict:
    if "agent.tasks:read" in principal.scopes:
        return task
    result = dict(task, argv=["[redacted]"], workdir=".", result={}, artifact_ref={})
    result.pop("gpu_assignment", None)
    result.pop("workspace", None)
    result.pop("workspace_result", None)
    return result
