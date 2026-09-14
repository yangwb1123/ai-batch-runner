"""GPU inventory projection and card assignment for Fabric Hub tasks."""

from __future__ import annotations

import time

from .fabric_gpu import (GPU_UUID_RE, MAX_GPU_COUNT,
                         gpu_observation_fresh, normalize_gpu_timestamp,
                         select_gpu_assignment)
from .fabric_hub_contract import probe_is_fresh as _probe_is_fresh

_SCHEDULABLE_LIFECYCLES = {"ready", "reserved", "busy"}


class HubComputeGpuMixin:
    """Project fresh GPU facts and persist a server-owned assignment."""

    def _reserved_gpu_uuids(self, device_id: str,
                            ignore_task_id: str = "") -> set:
        """List UUIDs held by active task snapshots, including cancellation."""
        reserved = set()
        for task_id, task in self.tasks.items():
            if (task_id == ignore_task_id or task.get("device_id") != device_id
                    or (task.get("status") not in {
                        "queued", "leased", "running"}
                        and not task.get("cancel_requested"))):
                continue
            assignment = task.get("gpu_assignment", {})
            if isinstance(assignment, dict):
                uuids = assignment.get("uuids", [])
                if isinstance(uuids, list):
                    reserved.update(item for item in uuids
                                    if isinstance(item, str))
        return reserved

    def _gpu_inventory_projection(self, device_id: str,
                                  ignore_task_id: str = "") -> tuple[str, list]:
        """Build bounded schedulable rows from matched probe/heartbeat facts."""
        device = self.devices.get(device_id, {})
        if "task.gpu.v1" not in device.get("runner_capabilities", []):
            return "unsupported", []
        now = time.time()
        if not _gpu_device_fresh(device, now):
            return "stale", []
        static_gpus, dynamic_gpus = _gpu_fact_lists(device)
        if static_gpus is None or dynamic_gpus is None:
            return "unavailable", []
        return self._project_gpu_rows(
            device_id, static_gpus, dynamic_gpus, now, ignore_task_id)

    def _project_gpu_rows(self, device_id: str, static_gpus: list,
                          dynamic_gpus: list, now: float,
                          ignore_task_id: str) -> tuple[str, list]:
        """Validate identity, freshness and reservations before projection."""
        by_uuid = _gpu_probe_map(dynamic_gpus)
        if len(by_uuid) != len(dynamic_gpus):
            return "unavailable", []
        static_keys = [_gpu_uuid_key(row) for row in static_gpus]
        if (any(key is None for key in static_keys)
                or len(static_keys) != len(set(static_keys))):
            return "unavailable", []
        reserved = self._reserved_gpu_uuids(device_id, ignore_task_id)
        active = self._reserved_execution_slots(device_id, ignore_task_id) > 0
        allowed = (self.lifecycle.current(device_id)
                   in _SCHEDULABLE_LIFECYCLES
                   and device_id not in self.cordoned and not active)
        return _project_inventory_rows(
            static_gpus, by_uuid, reserved, allowed, now, active)

    def _select_gpu_assignment(self, device_id: str,
                               resources: dict) -> dict | None:
        status, rows = self._gpu_inventory_projection(device_id)
        if status != "available":
            return None
        return select_gpu_assignment(
            resources, rows, self._reserved_gpu_uuids(device_id), time.time())

    def _gpu_resource_admission_error(self, device_id: str, device: dict,
                                     resources: dict,
                                     ignore_task_id: str = "") -> str:
        if not resources.get("gpu_count", 0):
            return ""
        if "task.gpu.v1" not in device.get("runner_capabilities", []):
            return "target Runner does not support task.gpu.v1"
        status, _ = self._gpu_inventory_projection(device_id, ignore_task_id)
        if status != "available":
            return "requested GPU resources are temporarily unavailable"
        return ""


def _gpu_device_fresh(device: dict, now: float) -> bool:
    last_seen = device.get("last_seen", 0)
    return (not isinstance(last_seen, bool)
            and isinstance(last_seen, (int, float)) and last_seen > 0
            and -5 <= now - last_seen <= 90
            and _probe_is_fresh(device, now))


def _gpu_fact_lists(device: dict) -> tuple[list | None, list | None]:
    probe = device.get("probe", {})
    dynamic = device.get("dynamic_state", {})
    static = probe.get("static_capabilities", {}) if isinstance(probe, dict) else {}
    static_gpus = static.get("gpus", []) if isinstance(static, dict) else []
    dynamic_gpus = dynamic.get("gpus", []) if isinstance(dynamic, dict) else []
    if (not isinstance(static_gpus, list) or not isinstance(dynamic_gpus, list)
            or len(static_gpus) > MAX_GPU_COUNT
            or len(dynamic_gpus) > MAX_GPU_COUNT):
        return None, None
    return static_gpus, dynamic_gpus


def _project_inventory_rows(static_gpus: list, by_uuid: dict,
                            reserved: set, allowed: bool, now: float,
                            active: bool) -> tuple[str, list]:
    rows, stale = [], False
    for item in static_gpus:
        row, fresh = _gpu_inventory_row(item, by_uuid, reserved, allowed, now)
        if row is None:
            stale = True
        else:
            rows.append(row)
            stale |= not fresh
    if stale:
        status = "stale"
    elif active or reserved:
        status = "busy"
    elif any(row["schedulable"] for row in rows):
        status = "available"
    else:
        status = "unavailable"
    return status, rows


def _gpu_probe_map(gpus: list) -> dict:
    """Keep one bounded, syntactically valid dynamic fact per physical UUID."""
    result = {}
    duplicates = set()
    for item in gpus:
        if not isinstance(item, dict):
            continue
        uuid = _gpu_uuid_key(item)
        if uuid is None:
            continue
        if uuid in result:
            duplicates.add(uuid)
        result[uuid] = item
    return {uuid: item for uuid, item in result.items()
            if uuid not in duplicates}


def _gpu_uuid_key(value: dict) -> str | None:
    uuid = value.get("uuid") if isinstance(value, dict) else None
    if not isinstance(uuid, str) or not GPU_UUID_RE.fullmatch(uuid):
        return None
    return uuid.casefold()


def _gpu_inventory_row(static: dict, dynamic: dict, reserved: set,
                       device_schedulable: bool,
                       now: float) -> tuple[dict | None, bool]:
    if not isinstance(static, dict):
        return None, False
    uuid, total = static.get("uuid"), static.get("total_memory_bytes")
    if (not isinstance(uuid, str) or not GPU_UUID_RE.fullmatch(uuid)
            or static.get("vendor") != "nvidia"
            or not _valid_gpu_memory(total) or total == 0
            or not _valid_gpu_name(static.get("name"))):
        return None, False
    fact = dynamic.get(uuid.casefold())
    available = (fact.get("available_memory_bytes")
                 if isinstance(fact, dict) else None)
    if (not isinstance(fact, dict) or fact.get("vendor") != "nvidia"
            or not _valid_gpu_memory(fact.get("total_memory_bytes"))
            or fact.get("total_memory_bytes") != total
            or not _valid_gpu_memory(available) or available > total):
        return None, False
    observed_at = normalize_gpu_timestamp(fact.get("observed_at"))
    if observed_at is None:
        return None, False
    mig_mode = str(static.get("mig_mode", "")).lower()
    is_reserved = uuid.casefold() in {item.casefold() for item in reserved}
    fresh = gpu_observation_fresh(observed_at, now)
    schedulable = (device_schedulable and not is_reserved
                   and mig_mode in ("disabled", "n/a")
                   and fact.get("status") == "available" and fresh)
    return ({"uuid": uuid, "name": static["name"],
             "vendor": "nvidia", "total_memory_bytes": total,
             "available_memory_bytes": available,
             "schedulable": schedulable, "reserved": is_reserved,
             "observed_at": observed_at}, fresh)


def _valid_gpu_memory(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, int)
            and 0 <= value <= 1024 ** 4)


def _valid_gpu_name(value) -> bool:
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True
