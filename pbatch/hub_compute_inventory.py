"""Bounded inventory projection and deterministic authorized placement."""

from __future__ import annotations

from datetime import datetime
import time

from .hub_compute_gpu import gpu_eligible, gpu_inventory


def device_view(row: dict, binding: dict, *, gpu_supported: bool = False,
                workspace_supported: bool = False) -> dict:
    static = row.get("static_capabilities", {})
    dynamic = row.get("dynamic_state", {})
    resources = row.get("resources", {})
    capabilities = row.get("runner_capabilities", [])
    runtimes = row.get("runtimes", {})
    if isinstance(runtimes, dict):
        runtimes = [key for key, present in runtimes.items() if present]
    active = row.get("lifecycle_state") == "ready"
    free_slot = (row.get("reserved_execution_slots", 0) == 0
                 and resources.get("reserved_cpu_cores", 0) == 0
                 and dynamic.get("running_tasks", 0) == 0)
    schedulable = (row.get("online") is True and active and free_slot and not row.get("cordoned")
                   and _fresh(row.get("capability_valid_until", "")))
    return {
        "device_id": row["device_id"], "instance_id": binding["instance_id"],
        "workspace_supported": bool(workspace_supported and isinstance(capabilities, list)
                                     and "task.workspace.v1" in capabilities),
        "name": binding.get("name") or row.get("name", row["device_id"]),
        "online": row.get("online") is True, "schedulable": bool(schedulable),
        "lifecycle_state": row.get("lifecycle_state", "unknown"),
        "os": static.get("os", ""), "architecture": static.get("architecture", ""),
        "cpu_cores": _number(resources.get("cpu_cores", static.get("cpu_cores", 0))),
        "total_memory_bytes": _number(resources.get("memory_bytes", static.get("total_memory_bytes", 0))),
        "available_memory_bytes": _number(dynamic.get("available_memory_bytes", 0)),
        "reserved_cpu_cores": _number(resources.get("reserved_cpu_cores", 0)),
        "reserved_memory_bytes": _number(resources.get("reserved_memory_bytes", 0)),
        "runtimes": sorted(runtimes) if isinstance(runtimes, (list, dict)) else [],
        "running_tasks": _number(dynamic.get("running_tasks", 0)),
        "project_ids": sorted(binding["workspaces"]), "last_seen": row.get("last_seen", 0),
        **gpu_inventory(row, gpu_supported, bool(schedulable))}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0


def _fresh(value) -> bool:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() > time.time()
    except (AttributeError, TypeError, ValueError):
        return False


def eligible(task: dict, device: dict) -> bool:
    resources, required = task["resources"], task["requirements"]
    if not device["schedulable"] or task["project_id"] not in device["project_ids"]:
        return False
    if device["device_id"] not in task["_targets"]:
        return False
    if "workspace" in task and not device.get("workspace_supported", False):
        return False
    if task["target_device_id"] and task["target_device_id"] != device["device_id"]:
        return False
    if device["cpu_cores"] - device["reserved_cpu_cores"] < resources["cpu_cores"]:
        return False
    free_memory = min(device["available_memory_bytes"],
                      device["total_memory_bytes"] - device["reserved_memory_bytes"])
    if free_memory < resources["memory_bytes"]:
        return False
    if any(required[key] and device[key] != required[key] for key in ("os", "architecture")):
        return False
    return (set(required["runtimes"]) <= set(device["runtimes"])
            and gpu_eligible(resources, device))


def project_result(task: dict) -> dict:
    from .memory_records import redact_content
    evidence = task.get("evidence") or {}
    if not evidence:
        return {}
    result = {"exit_code": evidence.get("exit_code"), "timed_out": bool(evidence.get("timed_out")),
              "cancelled": bool(evidence.get("cancelled")), "stdout": "", "stderr": "",
              "output_truncated": bool(evidence.get("output_overflow")),
              "elapsed": evidence.get("elapsed_seconds", 0),
              "evidence_digest": task.get("evidence_digest", "")}
    for key in ("stdout", "stderr"):
        raw = str(redact_content(evidence.get(key, ""))).encode("utf-8")
        result[key] = raw[:8192].decode("utf-8", errors="ignore")
        result["output_truncated"] |= len(raw) > 8192
    return result
