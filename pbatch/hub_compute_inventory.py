"""Bounded inventory projection and deterministic authorized placement."""

from __future__ import annotations

from datetime import datetime
import math
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
    numbers = _resource_values(static, dynamic, resources)
    valid_facts = (all(_valid_number(value) for value in numbers.values())
                   and _valid_texts(runtimes)
                   and all(_valid_text(static.get(name, "")) for name in ("os", "architecture")))
    active = row.get("lifecycle_state") == "ready"
    free_slot = (row.get("reserved_execution_slots", 0) == 0
                 and resources.get("reserved_cpu_cores", 0) == 0
                 and dynamic.get("running_tasks", 0) == 0)
    schedulable = (valid_facts and row.get("online") is True and active and free_slot and not row.get("cordoned")
                   and _fresh(row.get("capability_valid_until", "")))
    return {
        "device_id": row["device_id"], "instance_id": binding["instance_id"],
        "workspace_supported": bool(workspace_supported and isinstance(capabilities, list)
                                     and "task.workspace.v1" in capabilities),
        "name": binding.get("name") or row.get("name", row["device_id"]),
        "online": row.get("online") is True, "schedulable": bool(schedulable),
        "lifecycle_state": row.get("lifecycle_state", "unknown"),
        "os": static.get("os", ""), "architecture": static.get("architecture", ""),
        **{name: _number(value) for name, value in numbers.items()},
        "runtimes": sorted(runtimes) if _valid_texts(runtimes) else [],
        "project_ids": sorted(binding["workspaces"]), "last_seen": row.get("last_seen", 0),
        **gpu_inventory(row, gpu_supported, bool(schedulable))}


def _number(value):
    return value if _valid_number(value) else 0


def _resource_values(static, dynamic, resources):
    return {
        "cpu_cores": resources.get("cpu_cores", static.get("cpu_cores", 0)),
        "total_memory_bytes": resources.get("memory_bytes", static.get("total_memory_bytes", 0)),
        "available_memory_bytes": dynamic.get("available_memory_bytes", 0),
        "reserved_cpu_cores": resources.get("reserved_cpu_cores", 0),
        "reserved_memory_bytes": resources.get("reserved_memory_bytes", 0),
        "running_tasks": dynamic.get("running_tasks", 0)}


def _valid_number(value) -> bool:
    try:
        return type(value) in {int, float} and value >= 0 and math.isfinite(value)
    except OverflowError:
        return False


def _valid_text(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


def _valid_texts(value) -> bool:
    return isinstance(value, list) and all(_valid_text(item) for item in value)


def _valid_projection(device) -> bool:
    numbers = {"cpu_cores", "total_memory_bytes", "available_memory_bytes",
               "reserved_cpu_cores", "reserved_memory_bytes", "running_tasks"}
    return (isinstance(device, dict)
            and all(_valid_number(device.get(name)) for name in numbers)
            and all(type(device.get(name)) is bool for name in ("schedulable", "online"))
            and type(device.get("workspace_supported", False)) is bool
            and all(_valid_text(device.get(name)) for name in
                    ("device_id", "instance_id", "os", "architecture", "lifecycle_state"))
            and all(_valid_texts(device.get(name)) for name in ("runtimes", "project_ids")))


def _fresh(value) -> bool:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() > time.time()
    except (AttributeError, TypeError, ValueError):
        return False


def eligible(task: dict, device: dict) -> bool:
    return not placement_reasons(task, device)


def placement_reasons(task: dict, device: dict) -> list[str]:
    """Explain the same projected facts used by the Hub placement filter."""
    if not _valid_projection(device):
        return ["device_unavailable"]
    resources, required = task["resources"], task["requirements"]
    reasons = _availability_reasons(device)
    if task["project_id"] not in device["project_ids"]:
        reasons.append("project_unavailable")
    if device["device_id"] not in task["_targets"]:
        reasons.append("target_not_authorized")
    if "workspace" in task and not device.get("workspace_supported", False):
        reasons.append("workspace_unsupported")
    if task["target_device_id"] and task["target_device_id"] != device["device_id"]:
        reasons.append("target_mismatch")
    if device["cpu_cores"] - device["reserved_cpu_cores"] < resources["cpu_cores"]:
        reasons.append("cpu_insufficient")
    free_memory = min(device["available_memory_bytes"],
                      device["total_memory_bytes"] - device["reserved_memory_bytes"])
    if free_memory < resources["memory_bytes"]:
        reasons.append("memory_insufficient")
    for key in ("os", "architecture"):
        if required[key] and device[key] != required[key]:
            reasons.append(key + "_mismatch")
    if not set(required["runtimes"]) <= set(device["runtimes"]):
        reasons.append("runtime_missing")
    reasons.extend(_gpu_reasons(resources, device))
    return reasons


def _gpu_reasons(resources: dict, device: dict) -> list[str]:
    if not resources.get("gpu_count", 0) or gpu_eligible(resources, device):
        return []
    return ["gpu_insufficient" if device.get("gpu_status") == "available" else "gpu_unavailable"]


def _availability_reasons(device: dict) -> list[str]:
    if device["schedulable"]:
        return []
    reasons = []
    if device.get("online") is not True:
        reasons.append("device_offline")
    if device.get("lifecycle_state") != "ready":
        reasons.append("device_not_ready")
    if device.get("reserved_cpu_cores", 0) > 0 or device.get("running_tasks", 0) > 0:
        reasons.append("device_busy")
    return reasons or ["device_unavailable"]


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
