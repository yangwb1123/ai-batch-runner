"""Validation and hashing primitives for Hub-originated Fabric requests."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime

from .fabric_gpu import normalize_gpu_resources
from .fabric_workspace import normalize_workspace_request, workspace_request_digest

MAX_DEVICE_TARGETS = 200
MAX_REQUEST_CPU_CORES = 256
MAX_REQUEST_MEMORY_BYTES = 16 * 1024 ** 4
MAX_TRACE_FIELD_CHARS = 256
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$")
TRACE_FIELDS = ("tenant_id", "instance_id", "session_id", "turn_id", "actor")
HEARTBEAT_STALE_SECONDS = 90


def _bounded_int(value, name: str, maximum: int, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"resources.{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"resources.{name} is outside supported bounds")
    return value


def normalize_resources(value) -> dict:
    """Validate a requested two-dimensional, bounded resource reservation."""
    required = {"cpu_cores", "memory_bytes"}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {"gpu_count", "gpu_memory_bytes"}):
        raise ValueError("resources must contain cpu_cores and memory_bytes")
    normalized = {
        "cpu_cores": _bounded_int(value["cpu_cores"], "cpu_cores",
                                  MAX_REQUEST_CPU_CORES),
        "memory_bytes": _bounded_int(value["memory_bytes"], "memory_bytes",
                                     MAX_REQUEST_MEMORY_BYTES, minimum=0),
    }
    if set(value) & {"gpu_count", "gpu_memory_bytes"}:
        normalized = normalize_gpu_resources({**normalized, **value})
    return normalized


def normalize_hub_context(value) -> dict:
    """Allow only bounded trace fields; this metadata never grants authority."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(TRACE_FIELDS):
        raise ValueError("hub_context contains unsupported fields")
    result = {}
    for key, item in value.items():
        empty_allowed = key == "turn_id"
        if (not isinstance(item, str)
                or (not item.strip() and not empty_allowed)
                or len(item) > MAX_TRACE_FIELD_CHARS
                or any(ord(char) < 32 for char in item)):
            raise ValueError(f"hub_context.{key} must be a bounded string")
        result[key] = item.strip()
    return result


def _request_id(value) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not REQUEST_ID_RE.fullmatch(value):
        raise ValueError("request_id must be 1..128 safe ASCII characters")
    return value


def _probe_expiry_epoch(value) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is not None:
        return parsed.timestamp()
    try:
        return time.mktime(parsed.timetuple())
    except (OverflowError, ValueError):
        return 0.0


def _workspace_options(payload: dict) -> tuple[dict | None, str]:
    value = payload.get("workspace")
    if value is None:
        return None, ""
    try:
        workspace = normalize_workspace_request(value)
    except ValueError as exc:
        return None, str(exc)
    if payload.get("mobility", "stateless") != "pinned":
        return None, "workspace tasks require pinned mobility"
    if payload.get("checkpoint_remote") or payload.get("checkpoint_blob"):
        return None, "workspace tasks cannot use checkpoint transfer"
    cwd = str(payload.get("cwd", ""))
    if not cwd.startswith("/") or ".." in cwd.split("/") or "\\" in cwd:
        return None, "workspace tasks require an absolute contained cwd"
    return workspace, ""


def capacity(device: dict) -> tuple[int, int]:
    probe = device.get("probe", {})
    static = probe.get("static_capabilities", {}) if isinstance(probe, dict) else {}
    if not isinstance(static, dict):
        return 0, 0
    cores, memory = static.get("cpu_cores"), static.get("total_memory_bytes")
    if (isinstance(cores, bool) or not isinstance(cores, int)
            or not 1 <= cores <= 4096):
        return 0, 0
    if (isinstance(memory, bool) or not isinstance(memory, int)
            or not 1 <= memory <= 16 * 1024 ** 5):
        return 0, 0
    return cores, memory


def probe_is_fresh(device: dict, now: float) -> bool:
    last_seen = device.get("last_seen", 0)
    probe = device.get("probe", {})
    return (isinstance(last_seen, (int, float)) and last_seen > 0
            and now - last_seen <= HEARTBEAT_STALE_SECONDS
            and _probe_expiry_epoch(
                probe.get("capability_valid_until", "")
                if isinstance(probe, dict) else "") > now)


def submission_options(payload: dict) -> tuple[dict, str]:
    try:
        request_id = _request_id(payload.get("request_id", ""))
        resources = (normalize_resources(payload["resources"])
                     if payload.get("resources") is not None else None)
        context = normalize_hub_context(payload.get("hub_context"))
        workspace, workspace_error = _workspace_options(payload)
    except ValueError as exc:
        return {}, str(exc)
    if workspace_error:
        return {}, workspace_error
    enabled = bool(request_id or resources is not None or context
                   or workspace is not None)
    if not enabled:
        return {"enabled": False, "payload": payload}, ""
    normalized = dict(payload)
    if resources is not None:
        normalized["resources"] = resources
    if workspace is not None:
        normalized["workspace"] = workspace
    normalized["hub_context"] = context
    try:
        timeout = float(normalized.get("timeout", 60) or 60)
    except (TypeError, ValueError):
        return {}, "timeout must be a finite number between 0 and 3600"
    if (isinstance(normalized.get("timeout"), bool)
            or not math.isfinite(timeout) or not 0 < timeout <= 3600):
        return {}, "timeout must be a finite number between 0 and 3600"
    normalized.update({"mobility": "pinned", "timeout": timeout,
                       "ttl_seconds": math.ceil(timeout) + 120})
    return {"enabled": True, "payload": normalized,
            "request_id": request_id, "resources": resources,
            "hub_context": context, "workspace": workspace,
            "timeout": timeout}, ""


def request_digest(target_id: str, payload: dict, request: dict,
                   options: dict) -> str:
    cost = float(payload.get("estimated_cost", 0) or 0)
    if not math.isfinite(cost):
        raise ValueError("estimated_cost must be a finite number")
    blob = payload.get("checkpoint_blob", "")
    if not isinstance(blob, str):
        raise ValueError("checkpoint_blob must be a string")
    identity = {
        "target_device_id": target_id, "argv": request["argv"],
        "cwd": request["cwd"], "timeout": options["timeout"],
        "effect_class": request["effect_class"],
        "data_class": request["data_class"],
        "mobility": payload.get("mobility", "stateless"),
        "checkpoint_remote": request["checkpoint_remote"],
        "checkpoint_blob_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest()
        if blob else "",
        "network_budget_bytes": request["network_budget_bytes"],
        "ttl_seconds": payload.get("ttl_seconds", 300),
        "estimated_cost": cost, "resources": options["resources"],
        "hub_context": options["hub_context"],
    }
    if options.get("workspace") is not None:
        identity["workspace_digest"] = workspace_request_digest(
            options["workspace"])
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
