"""Hub-facing, device-scoped compute admission helpers for Fabric."""

from __future__ import annotations

import hashlib
import time
import uuid

from .fabric_server_util import CHECKPOINT_MAX_B64
from .fabric_task_store import task_locked
from .fabric_hub_contract import (DEVICE_ID_RE as _DEVICE_ID_RE,
                                  MAX_DEVICE_TARGETS,
                                  capacity as _capacity,
                                  normalize_resources,
                                  probe_is_fresh as _probe_is_fresh,
                                  request_digest as _request_digest,
                                  submission_options as _submission_options)
from .fabric_hub_gpu import (HubComputeGpuMixin,
                             _SCHEDULABLE_LIFECYCLES)
from .fabric_workspace import HUB_CAPABILITY, RUNNER_CAPABILITY

_ACTIVE_TASK_STATES = {"queued", "leased", "running"}


class HubComputeMixin(HubComputeGpuMixin):
    """Control-plane helpers for bounded inventory and idempotent admission."""

    @staticmethod
    def _submit_error_metadata(error: str) -> tuple[str, bool]:
        """Classify application errors for Hub retry and terminal decisions."""
        if error.startswith("idempotency_conflict:"):
            return "idempotency_conflict", False
        retry_fragments = (
            "resource target has no fresh probe",
            "requested CPU exceeds", "requested memory exceeds",
            "tenant quota exhausted", "device quota exhausted",
            "execution slot is busy",
            "requested GPU resources are temporarily unavailable",
        )
        if any(fragment in error for fragment in retry_fragments):
            return "resource_busy", True
        invalid_fragments = (
            "resources.", "resources must", "request_id must",
            "hub_context.", "hub_context contains", "argv must",
            "checkpoint", "network budget", "timeout must",
            "workspace must", "workspace.sha256", "workspace input",
            "workspace bundle", "workspace outputs", "workspace tasks require",
            "workspace tasks cannot",
        )
        code = ("invalid_request" if any(
            fragment in error for fragment in invalid_fragments)
            else "admission_rejected")
        return code, False

    @staticmethod
    def normalize_hub_payload(payload: dict) -> tuple[dict, str]:
        """Apply request limits before general task validation."""
        options, error = _submission_options(payload)
        return (options.get("payload", {}), error)

    def prepare_hub_submission(self, actor_id: str, target_id: str,
                               payload: dict, request: dict) -> tuple[dict, str]:
        """Normalize optional Hub fields and derive their durable request key."""
        options, error = _submission_options(payload)
        if error or not options.get("enabled"):
            return options, error
        normalized_payload = options["payload"]
        try:
            digest = _request_digest(target_id, normalized_payload,
                                     request, options)
        except (TypeError, ValueError) as exc:
            if str(exc) == "estimated_cost must be a finite number":
                return {}, str(exc)
            return {}, "task request contains invalid values"
        normalized_payload["request_id"] = options["request_id"]
        normalized_payload["request_digest"] = digest
        request_id = options["request_id"]
        task_id = self.request_task_id(actor_id, request_id) if request_id else ""
        return {"enabled": True, "payload": normalized_payload,
                "request_id": request_id, "request_digest": digest,
                "task_id": task_id, "resources": options["resources"],
                "hub_context": options["hub_context"],
                "workspace": options.get("workspace")}, ""

    @staticmethod
    def request_task_id(actor_id: str, request_id: str) -> str:
        raw = f"{actor_id}\0{request_id}".encode("utf-8")
        return "t-" + hashlib.sha256(raw).hexdigest()[:24]

    def existing_request(self, request: dict) -> tuple[dict | None, str]:
        """Return an idempotent task replay or a same-key content conflict."""
        task_id = request.get("task_id", "")
        task = self.tasks.get(task_id) if task_id else None
        if task is None:
            return None, ""
        if task.get("request_digest") != request.get("request_digest"):
            return None, "request_id already used with different task content"
        return task, ""

    def _task_submission_plan(self, payload: dict) -> tuple[dict, str]:
        actor = str(payload.get("device_id", ""))
        if actor not in self._approval_ids():
            return {}, "device not approved"
        target = self._resolve_task_target(actor, payload.get("target_device", ""))
        if target not in self._approval_ids():
            return {}, "target device not approved"
        normalized, error = self.normalize_hub_payload(payload)
        if error:
            return {}, error
        request, error = self._validated_task_request(target, normalized)
        if error:
            return {}, error
        submission, error = self.prepare_hub_submission(
            actor, target, normalized, request)
        if error:
            return {}, error
        if submission.get("enabled"):
            prior, conflict = self.existing_request(submission)
            if conflict:
                return {}, "idempotency_conflict: " + conflict
            if prior is not None:
                return {"duplicate": prior}, ""
            normalized = submission["payload"]
        if error := self._workspace_submission_error(
                submission, target, normalized):
            return {}, error
        if target in self.cordoned:
            return {}, "device cordoned/draining"
        resources = submission.get("resources") if submission.get("enabled") else None
        if error := self._resource_admission_error(target, resources):
            return {}, error
        normalized.pop("gpu_assignment", None)
        if resources and resources.get("gpu_count", 0):
            assignment = self._select_gpu_assignment(target, resources)
            if assignment is None:
                return {}, "requested GPU resources are temporarily unavailable"
            normalized["gpu_assignment"] = assignment
        return self._validated_submission_plan(
            actor, target, normalized, request, submission)

    def _workspace_submission_error(self, submission: dict, target: str,
                                   payload: dict) -> str:
        if submission.get("workspace") is None:
            return ""
        return self._workspace_target_error(target, payload)

    def _validated_submission_plan(self, actor, target, payload, request,
                                   submission) -> tuple[dict, str]:
        error = (self._tenant_quota_check(target)
                 or self._device_quota_check(target))
        if error:
            return {}, error
        cost, spent, error = self._task_cost(target, payload)
        if error:
            return {}, error
        checkpoint = payload.get("checkpoint_blob", "")
        if isinstance(checkpoint, str) and len(checkpoint) > CHECKPOINT_MAX_B64:
            return {}, "checkpoint too large"
        task_id = submission.get("task_id") or "t-" + uuid.uuid4().hex[:12]
        return {"actor": actor, "target": target, "payload": payload,
                "request": request, "submission": submission,
                "task_id": task_id, "cost": cost, "spent": spent}, ""

    def _workspace_target_error(self, target: str, payload: dict) -> str:
        """Require a Runner that explicitly advertises workspace support."""
        if payload.get("checkpoint_remote") or payload.get("checkpoint_blob"):
            return "workspace tasks cannot use checkpoint transfer"
        capabilities = self.devices.get(target, {}).get("runner_capabilities", [])
        if RUNNER_CAPABILITY not in capabilities:
            return "target Runner lacks task.workspace.v1"
        return ""

    @task_locked
    def handle_task_submit(self, payload: dict) -> dict:
        plan, error = self._task_submission_plan(payload)
        if error:
            code, retryable = self._submit_error_metadata(error)
            message = (error.split(": ", 1)[1]
                       if error.startswith("idempotency_conflict:") else error)
            return {"task": None, "error": message,
                    "code": code, "retryable": retryable}
        if duplicate := plan.get("duplicate"):
            return {"task": self._task_view(
                duplicate, include_workspace_result=False), "duplicate": True}
        return self._commit_task_submission(plan)

    def _commit_task_submission(self, plan: dict) -> dict:
        target = plan["target"]
        error = self._reserve_device_for_task(target)
        if error:
            return {"task": None, "error": error}
        request = plan["request"]
        task_id = plan["task_id"]
        payload = plan["payload"]
        lease = self._issue_lease(
            target, {}, int(payload.get("ttl_seconds", 300) or 300),
            grant_context=self._grant_context(
                target, task_id, request["effect_class"],
                request["data_class"], request["cwd"],
                workspace=payload.get("workspace") is not None))
        self.leases[lease["lease_id"]] = lease
        self.task_leases[task_id] = lease["lease_id"]
        if error := self._store_checkpoint(task_id, payload):
            self._rollback_task_admission(task_id, lease["lease_id"], target)
            return {"task": None, "error": error}
        error = self._commit_task_admission(
            task_id, target, plan["actor"], payload, request, plan["cost"],
            lease, plan["spent"])
        if error:
            return {"task": None, "error": error}
        return {"task": self._task_view(
            self.tasks[task_id], include_workspace_result=False)}

    @staticmethod
    def _poll_limit(payload: dict) -> tuple[int, str]:
        value = payload.get("limit", 200)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
            return 0, "limit must be an integer between 1 and 200"
        return value, ""

    def _has_active_task(self, device_id: str) -> bool:
        return any(task.get("device_id") == device_id
                   and (task.get("status") in ("leased", "running")
                        or task.get("cancel_requested"))
                   for task in self.tasks.values())

    def _reserved_execution_slots(self, device_id: str,
                                  ignore_task_id: str = "") -> int:
        """Count active control-plane tasks using the runner's single slot."""
        return sum(1 for task_id, task in self.tasks.items()
                   if task_id != ignore_task_id
                   and task.get("device_id") == device_id
                   and (task.get("status") in _ACTIVE_TASK_STATES
                        or task.get("cancel_requested")))

    @task_locked
    def handle_task_poll(self, payload: dict) -> dict:
        device = str(payload.get("device_id", ""))
        limit, error = self._poll_limit(payload)
        if error:
            return {"tasks": [], "server_time": time.time(), "error": error}
        self._reschedule_stale()
        error = self._prepare_device_poll(device)
        if error:
            return {"tasks": [], "server_time": time.time(), "error": error}
        if limit > 1 and self._has_queued_workspace_task(device):
            limit = 1
        if limit == 1 and self._has_active_task(device):
            return {"tasks": [], "server_time": time.time(),
                    "cordoned": device in self.cordoned,
                    "draining": bool(self.cordoned.get(device, {}).get("drain"))}
        return self._claim_queued_tasks(device, limit)

    def _has_queued_workspace_task(self, device_id: str) -> bool:
        return any(task.get("status") == "queued"
                   and task.get("device_id") in (None, device_id)
                   and isinstance(task.get("workspace"), dict)
                   for task in self.tasks.values())

    def _claim_queued_tasks(self, device_id: str, limit: int) -> dict:
        granted = []
        cordon = self.cordoned.get(device_id, {})
        for task in self.tasks.values():
            view, error = self._claim_poll_task(task, device_id, cordon)
            if error:
                return {"tasks": granted, "server_time": time.time(),
                        "error": error}
            if view is not None:
                granted.append(view)
                if len(granted) >= limit:
                    break
        return {"tasks": granted, "server_time": time.time(),
                "cordoned": device_id in self.cordoned,
                "draining": bool(cordon.get("drain"))}

    def _claim_poll_task(self, task: dict, device_id: str,
                         cordon: dict) -> tuple[dict | None, str]:
        if (task.get("status") != "queued"
                or task.get("device_id") not in (None, device_id)):
            return None, ""
        if cordon.get("drain"):
            return None, ""
        if task.get("device_id") is None:
            error = self._assign_unowned_task(task, device_id)
            if error:
                return None, ""
        elif task.get("resources") is not None:
            if self._resource_admission_error(
                    device_id, task["resources"], task.get("task_id", "")):
                return None, ""
        if error := self._mark_device_busy(device_id):
            return None, error
        task["status"] = "leased"
        self._mark_task_attempt(task, task.get("lease_id", ""), "leased")
        self._persist_task(task)
        return self._task_view(task, include_workspace_input=True), ""

    def _assign_unowned_task(self, task: dict, device_id: str) -> str:
        effect = str(task.get("effect_class", "read_only"))
        data_class = str(task.get("data_class", "public"))
        if self._task_policy_error(device_id, effect, data_class):
            return "device policy denied"
        if self._resource_admission_error(device_id, task.get("resources")):
            return "device resources unavailable"
        if error := self._reserve_task_quota(task, device_id):
            return error
        lease = self._issue_lease(
            device_id, {}, 300,
            grant_context=self._grant_context(
                device_id, task["task_id"], effect, data_class,
                str(task.get("cwd", ""))))
        self.leases[lease["lease_id"]] = lease
        self.task_leases[task["task_id"]] = lease["lease_id"]
        task.update({"lease_id": lease["lease_id"], "device_id": device_id,
                     "claimed_at": time.time()})
        self._append_task_attempt(task, lease, "leased")
        return ""

    @task_locked
    def handle_task_cancel(self, payload: dict) -> dict:
        task_id = str(payload.get("task_id", ""))
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": "unknown task"}
        if error := self._task_actor_error(task, payload.get("device_id")):
            return {"ok": False, "error": error}
        if task.get("status") == "running":
            self._request_running_cancel(task)
            return {"ok": True, "task_id": task_id,
                    "status": "running", "cancel_requested": True}
        if task.get("status") not in ("queued", "leased"):
            return {"ok": False, "error": f"task is {task.get('status')}"}
        self._cancel_task_record(task)
        self._sync_lifecycle_after_task(str(task.get("device_id", "")))
        return {"ok": True, "task_id": task_id}

    def _request_running_cancel(self, task: dict) -> None:
        if task.get("cancel_requested"):
            return
        task["cancel_requested"] = True
        task["cancel_requested_at"] = time.time()
        self._persist_task(task)

    def _reserved_resources(self, device_id: str,
                            ignore_task_id: str = "") -> dict:
        reserved = {"cpu_cores": 0, "memory_bytes": 0}
        for task_id, task in self.tasks.items():
            if (task_id == ignore_task_id or task.get("device_id") != device_id
                    or (task.get("status") not in _ACTIVE_TASK_STATES
                        and not task.get("cancel_requested"))
                    or task.get("quota_released")):
                continue
            raw = task.get("resources")
            if raw is None:
                continue
            try:
                request = normalize_resources(raw)
            except ValueError:
                request = {"cpu_cores": MAX_REQUEST_CPU_CORES,
                           "memory_bytes": MAX_REQUEST_MEMORY_BYTES}
            for key in reserved:
                reserved[key] += request[key]
        return reserved

    def _resource_admission_error(self, device_id: str, resources: dict,
                                  ignore_task_id: str = "") -> str:
        if resources is None:
            return ""
        device = self.devices.get(device_id, {})
        now = time.time()
        state = self.lifecycle.current(device_id)
        dynamic = device.get("dynamic_state", {})
        if (device_id in self.cordoned or state not in _SCHEDULABLE_LIFECYCLES
                or not isinstance(dynamic, dict)
                or dynamic.get("status") != "online"):
            return "resource target is offline or cordoned"
        if self._reserved_execution_slots(device_id, ignore_task_id):
            return "resource target execution slot is busy"
        if not _probe_is_fresh(device, now):
            return "resource target has no fresh probe/heartbeat"
        dynamic = device.get("dynamic_state", {})
        available = dynamic.get("available_memory_bytes")
        if (isinstance(available, bool) or not isinstance(available, int)
                or available < 0):
            return "resource target omitted bounded available memory"
        total_cpu, total_memory = _capacity(device)
        if not total_cpu or not total_memory:
            return "resource target omitted bounded CPU/memory capacity"
        if available > total_memory:
            return "resource target has invalid available memory"
        if resources["memory_bytes"] > available:
            return "requested memory exceeds currently available memory"
        reserved = self._reserved_resources(device_id, ignore_task_id)
        if reserved["cpu_cores"] + resources["cpu_cores"] > total_cpu:
            return "requested CPU exceeds unreserved device capacity"
        if reserved["memory_bytes"] + resources["memory_bytes"] > total_memory:
            return "requested memory exceeds unreserved device capacity"
        return self._gpu_resource_admission_error(
            device_id, device, resources, ignore_task_id)

    @task_locked
    def handle_inventory(self, payload: dict) -> dict:
        """Return only approved requested devices and their bounded resources."""
        actor = str(payload.get("device_id", ""))
        if actor not in self._approval_ids():
            return {"devices": [], "error": "device not approved"}
        target_ids = payload.get("target_device_ids")
        if (not isinstance(target_ids, list)
                or not 1 <= len(target_ids) <= MAX_DEVICE_TARGETS
                or any(not isinstance(item, str)
                       or not _DEVICE_ID_RE.fullmatch(item)
                       for item in target_ids)
                or len(set(target_ids)) != len(target_ids)):
            return {"devices": [], "error": "target_device_ids must contain 1..200 distinct device IDs"}

        self._reschedule_stale()
        status = {item["device_id"]: item for item in self.status_view()}
        devices = []
        approved = self._approval_ids()
        for device_id in target_ids:
            if device_id not in approved or device_id not in status:
                continue
            row = dict(status[device_id])
            source = self.devices.get(device_id, {})
            dynamic = source.get("dynamic_state", {})
            dynamic_view = dict(dynamic) if isinstance(dynamic, dict) else {}
            dynamic_view.pop("gpus", None)
            gpu_status, gpu_rows = self._gpu_inventory_projection(device_id)
            row["gpu_status"] = gpu_status
            row["gpus"] = gpu_rows
            row["dynamic_state"] = dynamic_view
            static_view = row.get("static_capabilities", {})
            if isinstance(static_view, dict) and "gpus" in static_view:
                row["static_capabilities"] = {
                    key: value for key, value in static_view.items()
                    if key != "gpus"}
            capacity_cpu, capacity_memory = _capacity(source)
            reserved = self._reserved_resources(device_id)
            row["resources"] = {
                "cpu_cores": capacity_cpu,
                "memory_bytes": capacity_memory,
                "reserved_cpu_cores": reserved["cpu_cores"],
                "reserved_memory_bytes": reserved["memory_bytes"],
            }
            row["execution_slots"] = 1
            row["reserved_execution_slots"] = self._reserved_execution_slots(
                device_id)
            devices.append(row)
        return {"devices": devices, "server_time": time.time(),
                "capabilities": ["hub.compute.v1", "hub.compute.gpu.v1",
                                 HUB_CAPABILITY]}
