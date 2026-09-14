"""Bounded GPU DTOs at the Hub/Fabric boundary; Fabric owns card assignment."""

from __future__ import annotations

import math
import re
import time

from .fabric_gpu import normalize_gpu_assignment
from .hub_compute_models import TERMINAL, integer
from .hub_errors import HubAccessError

GPU_CAPABILITY = "hub.compute.gpu.v1"
GPU_MAX_AGE = 90
GPU_STATES = frozenset({"unsupported", "unavailable", "stale", "available", "busy"})
GPU_UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
GPU_FIELDS = frozenset({"uuid", "name", "vendor", "total_memory_bytes",
                        "available_memory_bytes", "schedulable", "reserved", "observed_at"})


def gpu_inventory(row: dict, supported: bool, schedulable: bool) -> dict:
    if not supported:
        return {"gpu_status": "unsupported", "gpus": []}
    status, rows = row.get("gpu_status", "unsupported"), row.get("gpus", [])
    if (not isinstance(status, str) or status not in GPU_STATES
            or not isinstance(rows, list) or len(rows) > 16):
        return {"gpu_status": "unavailable", "gpus": []}
    try:
        cards = [_gpu_card(card) for card in rows]
        if len({card["uuid"].lower() for card in cards}) != len(cards):
            raise ValueError("duplicate GPU UUID")
    except (TypeError, ValueError):
        return {"gpu_status": "unavailable", "gpus": []}
    now = time.time()
    stale = any(not -5 <= now - card["observed_at"] <= GPU_MAX_AGE for card in cards)
    if stale:
        status = "stale"
    elif status == "available" and not cards:
        status = "unavailable"
    for card in cards:
        card["schedulable"] &= (schedulable and status == "available"
                                and not card["reserved"])
    return {"gpu_status": status, "gpus": cards}


def _gpu_card(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != GPU_FIELDS:
        raise ValueError("invalid GPU fields")
    _uuid(value["uuid"])
    name = value["name"]
    if (value["vendor"] != "nvidia" or not isinstance(name, str) or not 1 <= len(name) <= 128
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError("invalid GPU description")
    name.encode("utf-8")
    total = integer(value["total_memory_bytes"], "GPU total memory", 1, 1024**4)
    integer(value["available_memory_bytes"], "GPU available memory", 0, total)
    if any(not isinstance(value[key], bool) for key in ("reserved", "schedulable")):
        raise ValueError("invalid GPU availability")
    _timestamp(value["observed_at"])
    return dict(value)


def gpu_eligible(resources: dict, device: dict) -> bool:
    count = resources.get("gpu_count", 0)
    if not count:
        return True
    if device.get("gpu_status") != "available":
        return False
    memory = resources.get("gpu_memory_bytes", 0)
    return sum(card["schedulable"] and not card["reserved"]
               and card["available_memory_bytes"] >= memory
               for card in device.get("gpus", [])) >= count


def gpu_assignment(remote: dict, task: dict) -> dict:
    """Accept only a complete, immutable assignment matching the stored request."""
    if not task["resources"].get("gpu_count", 0):
        return {}
    value = remote.get("gpu_assignment")
    try:
        result = normalize_gpu_assignment(value, task["resources"]["gpu_count"],
                                           task["resources"]["gpu_memory_bytes"])
        prior = task.get("gpu_assignment")
        if prior and result != prior:
            raise ValueError("GPU assignment changed")
    except (TypeError, ValueError, KeyError) as exc:
        raise HubAccessError("gpu_assignment_invalid", "Fabric GPU assignment is invalid", 503) from exc
    return result


def gpu_observation(remote: dict, task: dict, state: str) -> tuple[str, dict]:
    """Retain cancellation and terminal cleanup when GPU evidence is untrusted."""
    invalid = task.get("_gpu_untrusted", False)
    changes = {}
    try:
        assignment = gpu_assignment(remote, task)
        if assignment:
            changes["gpu_assignment"] = assignment
    except HubAccessError:
        invalid = True
    if invalid:
        changes.update(error="gpu_assignment_invalid", result={}, _gpu_untrusted=True)
        state = "failed" if state == "completed" else state
        if state not in TERMINAL:
            state = "cancel_requested"
    return state, changes


def _uuid(value) -> None:
    if not isinstance(value, str) or not GPU_UUID.fullmatch(value):
        raise ValueError("invalid physical GPU UUID")


def _timestamp(value) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 < value <= 10**12 or not math.isfinite(value)):
        raise ValueError("invalid GPU timestamp")
