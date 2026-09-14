"""Bounded presentation of compute sessions, GPU devices and task evidence."""

from __future__ import annotations

import json
from typing import Any

MAX_DEVICES_SHOWN = 200
MAX_TASK_OUTPUT_CHARS = 4096


def _id(row: dict[str, Any], field: str) -> str:
    value = row.get(field, "")
    return str(value) if isinstance(value, (str, int)) else ""


def _permission_error(action: str, scope: str, exc: Exception) -> str:
    code = str(getattr(exc, "code", ""))
    status = int(getattr(exc, "status", 0) or 0)
    if status in {401, 403} or code in {"forbidden", "scope_required"}:
        return f"{action}: permission denied; check {scope} and instance access ({code or status})."
    return f"{action}: {type(exc).__name__}" + (f" ({code})" if code else "")


def _session_label(row: dict[str, Any]) -> str:
    status = _id(row, "status") or "unknown"
    access = "ready" if row.get("controllable") is True else "read only/offline"
    return f"{_id(row, 'name') or 'session'} · {_id(row, 'session_id')} · {status} · {access}"


def _device_label(row: dict[str, Any]) -> str:
    cpu = max(0, int(row.get("cpu_cores", 0) or 0) -
              int(row.get("reserved_cpu_cores", 0) or 0))
    memory = max(0, int(row.get("available_memory_bytes", 0) or 0))
    return (f"{_id(row, 'name') or _id(row, 'device_id')} · "
            f"{_id(row, 'device_id')} · {cpu} CPU · {_memory_label(memory)} free · "
            f"{_gpu_label(row)}")


def _gpu_label(row: dict[str, Any]) -> str:
    devices = row.get("gpus", [])
    devices = devices if isinstance(devices, list) else []
    free = [gpu for gpu in devices[:16] if isinstance(gpu, dict)
            and gpu.get("schedulable") is True and gpu.get("reserved") is False]
    if not free:
        return "GPU: " + str(row.get("gpu_status", "unavailable"))[:32]
    memory = min(int(gpu.get("available_memory_bytes", 0)) for gpu in free)
    return f"{len(free)} GPU free (minimum {_memory_label(memory)} per card)"


def _device_summary(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No project devices are visible. Check agent.devices:read and project bindings."
    lines = ["Project devices (schedulable devices can be pinned):"]
    for row in rows[:MAX_DEVICES_SHOWN]:
        state = "ready" if row.get("schedulable") is True else "not schedulable"
        lines.append(f"{state} · {_device_label(row)} · "
                     f"{_memory_label(max(0, int(row.get('total_memory_bytes', 0) or 0)))} total")
    if len(rows) > MAX_DEVICES_SHOWN:
        lines.append(f"… {len(rows) - MAX_DEVICES_SHOWN} more devices hidden")
    return "\n".join(lines)


def _memory_label(value: int) -> str:
    if value <= 0:
        return "0 MiB"
    return f"{value / 1048576:.0f} MiB"


def _task_label(row: dict[str, Any]) -> str:
    return f"{_id(row, 'state') or 'unknown'} · {_id(row, 'task_id')}"


def _task_detail(task: dict[str, Any]) -> str:
    view = {key: task.get(key) for key in (
        "task_id", "session_id", "project_id", "state", "device_id",
        "instance_id", "argv", "workdir", "timeout", "resources",
        "created_at", "updated_at", "archive_state", "artifact_ref", "error", "gpu_assignment", "workspace", "workspace_result")
        if key in task}
    view["result"] = _result_view(task.get("result", {}))
    raw = json.dumps(view, ensure_ascii=False, indent=2, default=str)
    if len(raw) > 12288:
        raw = raw[:12288] + "\n… detail truncated"
    return raw


def _result_view(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"summary": str(value)[:512]}
    result = {key: value[key] for key in (
        "exit_code", "timed_out", "cancelled", "output_truncated", "elapsed",
        "evidence_digest") if key in value}
    for key in ("stdout", "stderr"):
        text = str(value.get(key, ""))
        result[key] = text[:MAX_TASK_OUTPUT_CHARS]
        if len(text) > MAX_TASK_OUTPUT_CHARS:
            result[key + "_truncated"] = True
    return result
