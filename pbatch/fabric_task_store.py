"""Durable task snapshots and restart recovery for the device control plane."""

from __future__ import annotations

import json
from functools import wraps
import os
import time
from pathlib import Path

from .fabric_gpu import normalize_gpu_assignment
from .fabric_workspace import workspace_task_projection


MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024


def task_locked(method):
    """Serialize control-plane task mutations through the task lock."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._task_lock:
            return method(self, *args, **kwargs)
    return guarded


class TaskStateError(ValueError):
    """Raised when durable task state is malformed or cannot be written."""


class TaskStateStore:
    """Bounded append-only snapshots; the last snapshot wins per task id."""

    def __init__(self, state_dir, filename: str = "tasks.jsonl") -> None:
        self.path = Path(state_dir) / filename

    @staticmethod
    def _line(event: dict) -> bytes:
        line = (json.dumps(event, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > MAX_RECORD_BYTES:
            raise TaskStateError("task snapshot too large")
        return line

    def load(self) -> dict[str, dict]:
        """Read latest snapshots with bounded records and total bytes."""
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_size > MAX_FILE_BYTES:
            raise TaskStateError("task state path is unsafe or too large")
        latest = {}
        with self.path.open("rb") as handle:
            for raw in iter(lambda: handle.readline(MAX_RECORD_BYTES + 1), b""):
                if len(raw) > MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                    raise TaskStateError("task state contains oversized record")
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise TaskStateError("task state contains invalid JSON") from exc
                if not isinstance(event, dict) or not isinstance(
                        event.get("task"), dict):
                    raise TaskStateError("task snapshot must contain task object")
                task_id = str(event.get("task_id", ""))
                if not task_id or str(event["task"].get("task_id", "")) != task_id:
                    raise TaskStateError("task snapshot has invalid task id")
                latest[task_id] = {"task": dict(event["task"]),
                                   "lease": dict(event["lease"])
                                   if isinstance(event.get("lease"), dict) else None}
        return latest

    def save(self, task: dict, lease: dict | None = None) -> None:
        """Append one fsynced snapshot and compact when the file is full."""
        task_id = str(task.get("task_id", "")) if isinstance(task, dict) else ""
        if not task_id:
            raise TaskStateError("task snapshot missing task id")
        event = {"task_id": task_id, "task": dict(task),
                 "lease": dict(lease) if isinstance(lease, dict) else None}
        line = self._line(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = self.path.stat().st_size if self.path.exists() else 0
        if current + len(line) > MAX_FILE_BYTES:
            latest = self.load()
            latest[task_id] = event
            self.compact(latest)
            return
        try:
            with self.path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise TaskStateError(f"cannot append task state: {exc}") from exc

    def compact(self, snapshots: dict[str, dict]) -> None:
        """Atomically replace the log with the supplied latest snapshots."""
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with temp.open("wb") as handle:
                for task_id, snapshot in snapshots.items():
                    event = snapshot if "task_id" in snapshot else {
                        "task_id": task_id, **snapshot}
                    line = self._line(event)
                    if handle.tell() + len(line) > MAX_FILE_BYTES:
                        raise TaskStateError("compacted task state exceeds file limit")
                    handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except (OSError, TaskStateError) as exc:
            try:
                temp.unlink()
            except OSError:
                pass
            raise TaskStateError(f"cannot compact task state: {exc}") from exc


class TaskStateMixin:
    """Control-plane hooks for persisting and recovering task snapshots."""

    def _store_task(self, task_id: str, device_id: str, payload: dict,
                    request: dict, cost: float, lease: dict) -> None:
        self.tasks[task_id] = {
            "task_id": task_id, "device_id": device_id,
            "argv": request["argv"], "estimated_cost": cost,
            "cwd": request["cwd"],
            "timeout": float(payload.get("timeout", 60) or 60),
            "effect_class": request["effect_class"],
            "data_class": request["data_class"],
            "network_budget_bytes": request["network_budget_bytes"],
            "request_id": str(payload.get("request_id", "")),
            "request_digest": str(payload.get("request_digest", "")),
            "resources": dict(payload["resources"])
            if isinstance(payload.get("resources"), dict) else None,
            "workspace": dict(payload["workspace"])
            if isinstance(payload.get("workspace"), dict) else None,
            "gpu_assignment": dict(payload["gpu_assignment"])
            if isinstance(payload.get("gpu_assignment"), dict) else None,
            "hub_context": dict(payload.get("hub_context", {}))
            if isinstance(payload.get("hub_context"), dict) else {},
            "mobility": str(payload.get("mobility", "stateless")),
            "quota_device_id": device_id, "quota_released": False,
            "checkpoint_remote": request["checkpoint_remote"],
            "status": "queued", "lease_id": lease["lease_id"],
            "cancel_requested": False,
            "created_at": time.time(), "evidence": None,
            "evidence_digest": "", "usage": None,
            "usage_ledger_id": "", "attempts": [],
        }

    def _persist_task(self, task: dict) -> None:
        lease = self.leases.get(str(task.get("lease_id", "")))
        self.task_store.save(task, lease)

    def _persist_task_result(self, task: dict) -> str:
        try:
            self._persist_task(task)
        except TaskStateError as exc:
            return f"task state: {exc}"
        return ""

    def _append_task_attempt(self, task: dict, lease: dict,
                             status: str = "leased") -> None:
        attempts = task.setdefault("attempts", [])
        lease_id = str(lease.get("lease_id", ""))
        for attempt in attempts:
            if attempt.get("lease_id") == lease_id:
                attempt["status"] = status
                return
        attempts.append({"lease_id": lease_id,
                         "device_id": str(lease.get("device_id", "")),
                         "epoch": lease.get("epoch", 0),
                         "status": status, "started_at": time.time()})
        if len(attempts) > 64:
            del attempts[:-64]

    def _mark_task_attempt(self, task: dict, lease_id: str, status: str,
                           reason: str = "") -> None:
        for attempt in task.get("attempts", []):
            if attempt.get("lease_id") == lease_id:
                attempt["status"] = status
                if status in ("verified", "failed", "lost", "cancelled"):
                    attempt["finished_at"] = time.time()
                else:
                    attempt.pop("finished_at", None)
                if reason:
                    attempt["reason"] = reason[:200]
                return

    @staticmethod
    def _task_actor_error(task: dict, actor_id: str) -> str:
        """Allow the submitting principal or current execution device only."""
        actor = str(actor_id or "")
        allowed = {str(task.get("owner_device_id", "")),
                   str(task.get("device_id", ""))}
        return "" if actor and actor in allowed else "task access denied"

    def _release_task_quota(self, task: dict) -> None:
        """Release one task's tenant/device reservation exactly once."""
        if task.get("quota_released"):
            return
        device_id = str(task.get("quota_device_id", task.get("device_id", "")))
        if device_id:
            self._tenant_quota_dec(device_id)
            self._device_quota_dec(device_id)
        task["quota_released"] = True

    def _reserve_task_quota(self, task: dict, device_id: str) -> str:
        """Acquire quota for a rescheduled task before issuing its lease."""
        if not task.get("quota_released"):
            return ""
        if error := self._tenant_quota_check(device_id):
            return error
        if error := self._device_quota_check(device_id):
            return error
        self._tenant_quota_inc(device_id)
        self._device_quota_inc(device_id)
        task["quota_device_id"] = device_id
        task["quota_released"] = False
        return ""

    def _cancel_task_record(self, task: dict, actor: str = "",
                            reason: str = "") -> None:
        """Persist cancellation and release its active quota reservation."""
        task["status"] = "cancelled"
        task["cancel_requested"] = False
        task["cancelled_at"] = time.time()
        if actor:
            task["cancelled_by"] = actor
        self._mark_task_attempt(task, task.get("lease_id", ""),
                                "cancelled", reason)
        self._release_task_quota(task)
        self._persist_task(task)

    def _rollback_task_admission(self, task_id: str, lease_id: str,
                                 device_id: str) -> None:
        """Undo an admission whose durable task snapshot was not committed."""
        had_checkpoint = task_id in self.checkpoints
        self.tasks.pop(task_id, None)
        self.task_leases.pop(task_id, None)
        self.leases.pop(lease_id, None)
        self._sync_lifecycle_after_task(device_id)
        if had_checkpoint:
            self._delete_checkpoint(task_id)

    def _commit_task_admission(self, task_id: str, device_id: str,
                               actor_id: str, payload: dict, request: dict,
                               cost: float, lease: dict, spent: float) -> str:
        """Commit budget, quota and the task snapshot as one admission unit."""
        self.device_budget_spent[device_id] = spent + cost
        self._tenant_quota_inc(device_id)
        self._device_quota_inc(device_id)
        self._store_task(task_id, device_id, payload, request, cost, lease)
        self.tasks[task_id]["owner_device_id"] = actor_id
        self._append_task_attempt(self.tasks[task_id], lease, "reserved")
        try:
            self._persist_task(self.tasks[task_id])
        except TaskStateError as exc:
            task = self.tasks[task_id]
            self._release_task_quota(task)
            self.device_budget_spent[device_id] = spent
            self._rollback_task_admission(task_id, lease["lease_id"], device_id)
            return f"task state: {exc}"
        self._record_event(actor_id, "submit", "task", task_id,
                           attempt=lease["lease_id"])
        return ""

    def _transfer_task_quota(self, task: dict, device_id: str) -> str:
        """Move a task reservation between devices during migration."""
        target = str(device_id or "")
        old = str(task.get("quota_device_id", task.get("device_id", "")))
        held = not task.get("quota_released")
        if held and old == target:
            return ""
        if held:
            self._release_task_quota(task)
        error = self._tenant_quota_check(target) or self._device_quota_check(
            target)
        if error:
            if held and old:
                self._tenant_quota_inc(old)
                self._device_quota_inc(old)
                task["quota_device_id"] = old
                task["quota_released"] = False
            return error
        self._tenant_quota_inc(target)
        self._device_quota_inc(target)
        task["quota_device_id"] = target
        task["quota_released"] = False
        return ""

    def _task_view(self, task: dict, *, include_workspace_input=False,
                   include_workspace_result=True) -> dict:
        view = {key: task.get(key) for key in (
            "task_id", "device_id", "argv", "cwd", "timeout",
            "effect_class", "mobility", "checkpoint_remote", "status",
            "data_class", "network_budget_bytes", "owner_device_id",
            "request_id", "resources", "hub_context", "cancel_requested",
            "created_at", "finished_at", "failure_reason", "started_at",
            "evidence",
            "evidence_digest", "usage", "usage_ledger_id", "attempts",
            "rescheduled_at", "lost_at")}
        if isinstance(task.get("gpu_assignment"), dict):
            view["gpu_assignment"] = dict(task["gpu_assignment"])
        view = workspace_task_projection(
            task, view, include_input=include_workspace_input,
            include_result=include_workspace_result)
        view["lease"] = self.leases.get(task.get("lease_id", ""))
        return view

    @staticmethod
    def _network_budget_error(task: dict, evidence: dict) -> str:
        """Turn an observed network-byte overrun into a terminal task failure."""
        try:
            limit = int(task.get("network_budget_bytes", 0) or 0)
            observed = float(evidence.get("network_bytes", 0) or 0)
        except (TypeError, ValueError):
            return ""
        if limit > 0 and observed > limit:
            return f"network budget exceeded ({observed:.0f} > {limit} bytes)"
        return ""

    def _finish_task_evidence(self, task: dict, evidence: dict,
                              digest: str, legacy: bool, usage: dict,
                              network_error: str,
                              cancelled: bool = False) -> None:
        """Apply terminal evidence facts after validation and ledger append."""
        if cancelled and task.get("cancel_requested"):
            task["status"] = "cancelled"
        else:
            task["status"] = ("done" if evidence.get("result") == "passed"
                               and not network_error else "failed")
        task["cancel_requested"] = False
        task["evidence"] = evidence
        task["evidence_digest"] = digest
        task["evidence_legacy"] = legacy
        task["usage"] = usage["usage"]
        task["usage_ledger_id"] = usage["ledger_id"]
        if network_error:
            task["failure_reason"] = network_error
        task["finished_at"] = time.time()
        attempt_status = {"done": "verified", "cancelled": "cancelled",
                          "failed": "failed"}[task["status"]]
        self._mark_task_attempt(task, task.get("lease_id", ""),
                                attempt_status)
        self._release_task_quota(task)

    @task_locked
    def handle_task_start(self, payload: dict) -> dict:
        """Atomically promote a leased task before the Runner starts it."""
        task_id = str(payload.get("task_id", ""))
        device_id = str(payload.get("device_id", ""))
        lease_id = str(payload.get("lease_id", ""))
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": "unknown task"}
        lease, error = self._evidence_lease(
            device_id, task_id, lease_id, payload.get("fencing_token"))
        if error:
            return {"ok": False, "error": error}
        if task.get("cancel_requested"):
            return {"ok": False, "error": "task cancellation requested"}
        if task.get("status") == "running":
            return {"ok": True, "duplicate": True,
                    "task": self._task_view(task)}
        if task.get("status") != "leased":
            return {"ok": False, "error": f"task is {task.get('status')}"}
        resources = task.get("resources") or {}
        if isinstance(resources, dict) and resources.get("gpu_count", 0):
            if "task.gpu.v1" not in self.devices.get(
                    device_id, {}).get("runner_capabilities", []):
                return {"ok": False, "error": "Runner lacks task.gpu.v1"}
            try:
                normalize_gpu_assignment(
                    task.get("gpu_assignment"), resources["gpu_count"],
                    resources.get("gpu_memory_bytes", 0))
            except (KeyError, ValueError):
                return {"ok": False, "error": "GPU task assignment is invalid"}
        task["status"] = "running"
        task["started_at"] = time.time()
        self._mark_task_attempt(task, lease["lease_id"], "running")
        try:
            self._persist_task(task)
        except TaskStateError as exc:
            task["status"] = "leased"
            task.pop("started_at", None)
            self._mark_task_attempt(task, lease["lease_id"], "leased")
            return {"ok": False, "error": f"task state: {exc}"}
        return {"ok": True, "task": self._task_view(task)}

    @task_locked
    def handle_task_gang_submit(self, payload: dict) -> dict:
        """Submit a preflighted gang while claimers are excluded by the lock."""
        if payload.get("workspace") is not None:
            return self._gang_failure("workspace tasks do not support gang execution")
        return self._handle_legacy_gang_submit(payload)

    def _handle_legacy_gang_submit(self, payload: dict) -> dict:
        """Retain gang admission for the legacy non-workspace task contract."""
        actor = str(payload.get("device_id", ""))
        refs = payload.get("target_devices")
        if (not isinstance(refs, list) or len(refs) < 2
                or len({str(item).strip() for item in refs}) != len(refs)):
            return self._gang_failure("gang needs distinct target_devices")
        if actor not in self._approval_ids():
            return self._gang_failure("device not approved")
        targets = []
        tenant_need = {}
        for ref in refs:
            target = self._resolve_task_target(actor, ref)
            if target not in self._approval_ids():
                return self._gang_failure("target device not approved")
            if target in self.cordoned:
                return self._gang_failure("device cordoned/draining")
            request, error = self._validated_task_request(target, payload)
            if error:
                return self._gang_failure(error)
            if error := self._device_quota_check(target):
                return self._gang_failure(error)
            tenant = self._tenant_of(target)
            tenant_need[tenant] = tenant_need.get(tenant, 0) + 1
            quota = self.tenant_quotas.get(tenant)
            if quota is not None and self.tenant_running.get(tenant, 0) + \
                    tenant_need[tenant] > quota:
                return self._gang_failure("tenant quota exhausted")
            _, _, error = self._task_cost(target, payload)
            if error:
                return self._gang_failure(error)
            targets.append(target)
        created = []
        for target in targets:
            item = dict(payload)
            item.pop("target_devices", None)
            item["target_device"] = target
            result = self.handle_task_submit(item)
            task = result.get("task")
            if task is None:
                for previous in created:
                    self._cancel_task_record(self.tasks[previous["task_id"]],
                                             "gang", "gang rollback")
                return self._gang_failure(result.get("error", "gang failed"))
            created.append(task)
        return {"ok": True, "tasks": created}

    @staticmethod
    def _gang_failure(error: str) -> dict:
        """Return a stable failed-gang shape; callers never see partial tasks."""
        return {"ok": False, "error": str(error), "tasks": []}

    def _recover_tasks(self) -> None:
        """Restore terminal facts and invalidate in-flight work after restart."""
        snapshots = self.task_store.load()
        for task_id, snapshot in snapshots.items():
            task = snapshot["task"]
            self.tasks[task_id] = task
            self.device_budget_spent[str(task.get("device_id", ""))] = (
                self.device_budget_spent.get(str(task.get("device_id", "")), 0.0)
                + max(0.0, float(task.get("estimated_cost", 0) or 0)))
        for task in self.tasks.values():
            if task.get("status") in ("queued", "leased", "running"):
                self._recover_inflight_task(task)
            elif task.get("status") in ("done", "failed"):
                self._restore_terminal_lease(task, snapshots[task["task_id"]])
                self._release_task_quota(task)
            elif task.get("status") in ("cancelled", "lost"):
                self._release_task_quota(task)
            self._persist_task(task)

    def _recover_inflight_task(self, task: dict) -> None:
        """Turn a pre-restart attempt into a safe retry or explicit LOST."""
        self._release_task_quota(task)
        old_lease = str(task.get("lease_id", ""))
        if old_lease:
            self._mark_task_attempt(task, old_lease, "lost",
                                    "control plane restarted")
        if task.get("mobility") in ("stateless", "restartable"):
            task.update({"status": "queued", "device_id": None,
                         "lease_id": "", "cancel_requested": False,
                         "rescheduled_at": time.time()})
            self.task_leases[task["task_id"]] = ""
        else:
            task.update({"status": "lost", "lost_at": time.time()})

    def _restore_terminal_lease(self, task: dict, snapshot: dict) -> None:
        """Keep a terminal lease only so an identical duplicate can be audited."""
        lease = snapshot.get("lease")
        lease_id = str((lease or {}).get("lease_id", ""))
        if not lease_id:
            return
        self.leases[lease_id] = lease
        self.task_leases[task["task_id"]] = lease_id
        device_id = str(lease.get("device_id", ""))
        self.epochs[device_id] = max(self.epochs.get(device_id, 0),
                                     int(lease.get("epoch", 0) or 0))
