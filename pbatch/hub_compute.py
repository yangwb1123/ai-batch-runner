"""Restart-safe Hub orchestration over the existing Fabric execution plane."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath

from .hub_compute_config import connections
from .hub_compute_inventory import device_view, eligible, project_result
from .hub_compute_gpu import GPU_CAPABILITY, gpu_observation
from .hub_compute_models import TERMINAL
from .hub_compute_store import ComputeStore
from .hub_errors import HubAccessError
from .hub_store_helpers import identifier

log = logging.getLogger("pi-batch.hub-compute")


class HubCompute:
    def __init__(self, hub, settings=(), *, configured=None):
        self.store = ComputeStore(hub)
        self.connections = connections(list(settings)) if configured is None else configured
        from .hub_workspace import HubWorkspace
        self.workspace = HubWorkspace(self)
        self._stop = threading.Event()
        self._threads = []

    def connections_for(self, tenant: str) -> tuple:
        value = self.connections.get(tenant)
        if value is None:
            raise HubAccessError("compute_unavailable", "compute is not configured for this tenant", 503)
        rows = value if isinstance(value, (list, tuple)) else (value,)
        if not rows or len(rows) > 16:
            raise HubAccessError("compute_unavailable", "compute federation is invalid", 503)
        return tuple(rows)

    def bindings(self, tenant: str) -> dict:
        result = {}
        for connection in self.connections_for(tenant):
            for device, binding in connection.bindings.items():
                if device in result:
                    raise HubAccessError("compute_unavailable", "duplicate federated device binding", 503)
                result[device] = binding
        return result

    def connection(self, tenant: str):
        """Return the sole connection; federated callers must provide a device."""
        rows = self.connections_for(tenant)
        if len(rows) != 1:
            raise HubAccessError("compute_ambiguous", "a device is required for federated compute", 409)
        return rows[0]

    def connection_for_device(self, tenant: str, device_id: str):
        if not isinstance(device_id, str) or not device_id:
            raise HubAccessError("compute_unavailable", "a bound device is required", 503)
        for connection in self.connections_for(tenant):
            if device_id in connection.bindings:
                return connection
        raise HubAccessError("compute_unavailable", "task device is no longer configured", 503)

    def archive_connection(self, tenant: str, device_id: str = ""):
        """Select a Vault, rejecting ambiguous cross-plane archives."""
        if device_id:
            return self.connection_for_device(tenant, device_id)
        rows = [connection for connection in self.connections_for(tenant)
                if connection.archive is not None]
        if not rows:
            return None
        signatures = set()
        for item in rows:
            signature = tuple(getattr(item.archive, key, None)
                              for key in ("base_url", "tenant_id", "prefix"))
            signatures.add(signature if all(value is not None for value in signature)
                           else ("opaque-archive", id(item.archive)))
        if len(signatures) != 1:
            raise HubAccessError("workspace_ambiguous",
                                 "a target device is required when Fabric planes use different Vaults", 409)
        return rows[0]

    def archive_enabled(self, tenant: str) -> bool:
        return any(connection.archive is not None for connection in self.connections_for(tenant))

    def inventory(self, tenant: str, *, target_ids=None, allow_partial=False) -> list:
        all_bindings = self.bindings(tenant)
        targets = list(all_bindings) if target_ids is None else list(target_ids)
        if len(targets) > 200 or len(set(targets)) != len(targets) or any(
                target not in all_bindings for target in targets):
            raise ValueError("invalid bound inventory targets")
        owners = {device: self.connection_for_device(tenant, device) for device in targets}
        pages = []
        failures = []
        for connection in self.connections_for(tenant):
            selected = [device for device in targets if owners[device] is connection]
            try:
                pages.extend(self._inventory_page(connection, selected[offset:offset + 16])
                             for offset in range(0, len(selected), 16))
            except (AttributeError, HubAccessError, KeyError, OSError, OverflowError,
                    RuntimeError, TypeError, ValueError) as exc:
                if not allow_partial:
                    raise
                failures.append((connection, exc))
                log.warning("Fabric inventory unavailable for %s (%s)",
                            getattr(connection, "url", "configured endpoint"), type(exc).__name__)
        if failures and not pages:
            raise failures[0][1]
        observed = {row["device_id"]: row for page in pages for row in page}
        return [observed[device] for device in targets if device in observed]

    @staticmethod
    def _inventory_page(connection, targets):
        response = connection.client().inventory(targets)
        if response.get("error") or "hub.compute.v1" not in response.get("capabilities", []):
            raise HubAccessError("compute_unavailable", "Fabric inventory unavailable or incompatible", 503)
        rows = response.get("devices")
        if not isinstance(rows, list) or len(rows) > len(targets):
            raise ValueError("invalid Fabric inventory")
        gpu_supported = GPU_CAPABILITY in response.get("capabilities", [])
        workspace_supported = "hub.compute.workspace.v1" in response.get("capabilities", [])
        return [device_view(row, connection.bindings[row["device_id"]], gpu_supported=gpu_supported,
                            workspace_supported=workspace_supported) for row in rows
                if isinstance(row, dict) and row.get("device_id") in targets]

    def start(self):
        if self._threads:
            return
        for archive in (False, True):
            name = "hub-archive" if archive else "hub-compute"
            thread = threading.Thread(target=self._loop, args=(archive,), name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def close(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=120 if thread.name == "hub-archive" else 30)
            if thread.is_alive():
                raise RuntimeError("compute worker did not stop")

    def _loop(self, archive=False):
        if not archive:
            self._execution_loop()
            return
        while not self._stop.is_set():
            try:
                self.tick(archive=archive)
            except Exception as exc:
                log.warning("Compute reconciliation unavailable (%s)", type(exc).__name__)
            self._stop.wait(0.5)

    def _execution_loop(self):
        pending = {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="hub-dispatch") as pool:
            while not self._stop.is_set():
                pending = {key: future for key, future in pending.items() if not future.done()}
                try:
                    for task in self.store.pending(limit=8):
                        key = task["task_id"]
                        if len(pending) >= 4:
                            break
                        if key not in pending:
                            pending[key] = pool.submit(self._attempt, task)
                except Exception as exc:
                    log.warning("Compute queue unavailable (%s)", type(exc).__name__)
                self._stop.wait(0.2)

    def tick(self, *, archive=False):
        for task in self.store.pending(archive=archive):
            if self._stop.is_set():
                break
            self._attempt(task)

    def _attempt(self, task):
        try:
            self._reconcile(task)
        except Exception as exc:
            reason = exc.code if isinstance(exc, HubAccessError) else "reconciliation_unavailable:" + type(exc).__name__
            self.store.update(task["tenant"], task["task_id"], {"error": reason}, delay=5)

    def _reconcile(self, task: dict):
        if task["state"] in TERMINAL:
            self._archive(task)
            return
        if task["state"] == "queued":
            task = self._place(task)
            if task["state"] != "dispatching":
                return
        connection = self.connection_for_device(task["tenant"], task.get("device_id", ""))
        if task.get("_fabric_actor") and connection.client().device_id != task["_fabric_actor"]:
            raise HubAccessError("fabric_identity_changed", "restore the original Fabric service identity", 503)
        if not task["_fabric_id"]:
            task = self._dispatch(task, connection)
            if not task["_fabric_id"]:
                return
        self._observe(task, connection)

    def _place(self, task: dict) -> dict:
        candidates = [row for row in self.inventory(task["tenant"], allow_partial=True)
                      if eligible(task, row)]
        if not candidates:
            return self.store.update(task["tenant"], task["task_id"],
                                     {"error": "waiting_for_eligible_device"}, delay=3, expected=("queued",))
        chosen = min(candidates, key=lambda row: (
            row["reserved_cpu_cores"] / max(row["cpu_cores"], 1), row["device_id"]))
        connection = self.connection_for_device(task["tenant"], chosen["device_id"])
        root = connection.bindings[chosen["device_id"]]["workspaces"][task["project_id"]]
        return self.store.update(task["tenant"], task["task_id"],
                                 {"device_id": chosen["device_id"], "error": "",
                                  "device_instance_id": chosen["instance_id"],
                                  "_cwd": str(PurePosixPath(root) / task["workdir"]),
                                  "_fabric_actor": connection.client().device_id},
                                 state="dispatching", expected=("queued",))

    def _dispatch(self, task: dict, connection) -> dict:
        context = {key: task[key] for key in ("instance_id", "session_id", "turn_id", "actor")}
        context["tenant_id"] = task["tenant"]
        client = self._client(task, connection)
        workspace = self.workspace.dispatch(task, connection)
        task, uncertain = self.store.begin_dispatch(task["tenant"], task["task_id"])
        if task["state"] in TERMINAL:
            return task
        response = client.submit_task(
            task["argv"], cwd=task["_cwd"], timeout=task["timeout"], mobility="pinned",
            effect_class="irreversible", data_class="internal", ttl_seconds=task["timeout"] + 120,
            target_device=task["device_id"],
            request_id=task.get("_fabric_request_id") or task["task_id"],
            resources=task["resources"], hub_context=context,
            **workspace)
        remote = response.get("task") or {}
        if not remote.get("task_id"):
            if response.get("retryable") is False or response.get("status_code") in {400, 403, 404}:
                state = "cancelled" if task["state"] == "cancel_requested" else "failed"
                if uncertain:
                    state = "lost"
                return self.store.update(task["tenant"], task["task_id"],
                                         {"error": "dispatch_rejected"}, state=state)
            retryable = response.get("retryable") is True
            reason = "waiting_for_device" if retryable else "dispatch_unconfirmed"
            return self.store.update(task["tenant"], task["task_id"],
                                     {"error": reason, "_dispatch_uncertain": not retryable}, delay=3)
        # Persist the remote handle even when its assignment is invalid so it can be cancelled.
        remote_id = identifier(remote["task_id"], "Fabric task id")
        task = self.store.update(task["tenant"], task["task_id"],
                                 {"_fabric_id": remote_id, "error": ""})
        state, changes = gpu_observation(remote, task, task["state"])
        return self.store.update(task["tenant"], task["task_id"], changes, state=state)

    def _observe(self, task: dict, connection):
        current = self.store.get(task["tenant"], task["task_id"], private=True)
        fabric_id = task.get("_fabric_id", "")
        if not fabric_id or current.get("_fabric_id") != fabric_id:
            return
        client = self._client(task, connection)
        if current["state"] == "cancel_requested":
            client.task_cancel(fabric_id)
        response = client.task_status(fabric_id)
        remote = response.get("task") or {}
        if not remote:
            raise RuntimeError("Fabric task status unavailable")
        if (remote.get("status") == "lost"
                and current["state"] == "dispatching"
                and "started_at" in remote
                and remote["started_at"] is None):
            # A leased task that never reached Runner start has no observable
            # side effect.  Release the old remote handle and let the normal
            # placement loop choose another currently eligible device.  Once
            # start evidence exists, the task remains terminal lost so an
            # irreversible command is never duplicated implicitly.
            self.store.requeue_unstarted(
                task["tenant"], task["task_id"],
                reason="device_lost_before_start")
            return
        mapped = {"done": "completed", "failed": "failed", "cancelled": "cancelled",
                  "lost": "lost", "running": "running", "cancel_requested": "cancel_requested"}
        state = mapped.get(remote.get("status"), "dispatching")
        if current["state"] == "cancel_requested" and state not in TERMINAL:
            state = "cancel_requested"
        changes = {"result": project_result(remote), "error": ""}
        if state in {"failed", "lost"}:
            changes["error"] = "remote_" + state
        state, gpu_changes = gpu_observation(remote, current, state)
        changes.update(gpu_changes)
        state, workspace_changes = self.workspace.observe(current, remote, state)
        changes.update(workspace_changes)
        self.store.update(task["tenant"], task["task_id"], changes, state=state)

    @staticmethod
    def _client(task, connection):
        client = connection.client()
        if client.device_id != task.get("_fabric_actor"):
            raise HubAccessError("fabric_identity_changed", "restore the original Fabric service identity", 503)
        return client

    def _archive(self, task: dict, connection=None):
        if task["archive_state"] not in {"pending", "failed"}:
            return
        if connection is None:
            connection = (self.connection_for_device(task["tenant"], task["device_id"])
                          if task.get("device_id") else self.archive_connection(task["tenant"]))
        if connection is None or connection.archive is None:
            state = "failed" if "workspace" in task else "disabled"
            self.store.update(task["tenant"], task["task_id"], {"archive_state": state}, delay=60)
            return
        evidence = {key: task[key] for key in (
            "task_id", "session_id", "instance_id", "device_id", "state", "result")}
        if task.get("gpu_assignment"):
            evidence.update(resources=task["resources"], gpu_assignment=task["gpu_assignment"])
        try:
            workspace_changes = self.workspace.archive(task, connection)
            if workspace_changes:
                task = self.store.update(task["tenant"], task["task_id"], workspace_changes)
            if "workspace" in task:
                evidence["workspace"] = task["workspace"]
                evidence["workspace_result"] = task["workspace_result"]
            ref = connection.archive.archive(task["task_id"], evidence)
            self.store.update(task["tenant"], task["task_id"],
                              {"archive_state": "archived", "artifact_ref": ref})
        except Exception:
            self.store.update(task["tenant"], task["task_id"], {"archive_state": "failed"}, delay=60)
