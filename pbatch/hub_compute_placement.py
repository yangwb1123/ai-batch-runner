"""Read-only, bounded explanations of currently visible Hub placement filters."""

from __future__ import annotations

import json
import math
from urllib.parse import unquote, unquote_to_bytes

from .hub_compute_inventory import placement_reasons
from .hub_compute_models import fields, integer
from .hub_errors import HubAccessError
from .hub_store_helpers import identifier

MAX_RESPONSE_BYTES = 65536
STATES = frozenset({"queued", "dispatching", "running", "cancel_requested",
                    "completed", "failed", "cancelled", "lost"})


def is_route(parts) -> bool:
    return len(parts) == 3 and parts[0] == "tasks" and parts[2] == "placement"


def is_path(path) -> bool:
    """Recognize the new route, rejecting malformed UTF-8 only within it."""
    from .hub_service import PREFIX
    if not path.startswith(PREFIX + "/"):
        return False
    parts = [unquote(part) for part in path[len(PREFIX) + 1:].split("/")]
    if not is_route(parts):
        return False
    unquote_to_bytes(path).decode("utf-8")
    return True


def validate_read_headers(path, headers) -> None:
    if not is_path(path):
        return
    lengths = headers.get_all("Content-Length", [])
    if (headers.get_all("Transfer-Encoding", []) or len(lengths) > 1
            or any(value != "0" for value in lengths)):
        raise ValueError("placement reads do not accept a body")


def page_query(query: dict) -> dict:
    fields(query, {"after", "limit"})
    raw = query.get("limit", "20")
    if (not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal()
            or len(raw.lstrip("0")) > 2):
        raise ValueError("placement limit must be a decimal integer between 1 and 20")
    return page_values(query.get("after", ""), int(raw.lstrip("0") or "0"))


def page_values(after="", limit=20) -> dict:
    if after != "":
        identifier(after, "after").encode("utf-8")
    elif not isinstance(after, str):
        raise ValueError("placement cursor must be a string")
    return {"after": after, "limit": integer(limit, "limit", 1, 20)}


def read_placement(compute, principal, task_id, *, after="", limit=20) -> dict:
    page = page_values(after, limit)
    identifier(task_id, "task_id").encode("utf-8")
    principal.require("agent.tasks:read")
    principal.require("agent.devices:read")
    task = compute.store.get(principal.tenant_id, task_id, private=True)
    principal.require("agent.tasks:read", task["instance_id"])
    if task["state"] != "queued":
        return _result(compute, task, [], "")
    bindings = compute.bindings(principal.tenant_id)
    targets = sorted(device for device, binding in bindings.items()
                     if device in task["_targets"]
                     and principal.allows_compute(binding["instance_id"])
                     and task["project_id"] in binding["workspaces"]
                     and device > page["after"])
    chosen = targets[:page["limit"]]
    rows = _device_rows(compute, principal.tenant_id, task, bindings, chosen)
    latest = compute.store.get(principal.tenant_id, task_id, private=True)
    if latest["state"] != "queued":
        return _result(compute, latest, [], "")
    cursor = chosen[-1] if len(targets) > len(chosen) else ""
    return _result(compute, latest, rows, cursor)


def _device_rows(compute, tenant, task, bindings, targets) -> list:
    try:
        return _observe_devices(compute, tenant, task, bindings, targets)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise HubAccessError("placement_unavailable", "invalid placement inventory", 503) from exc


def _observe_devices(compute, tenant, task, bindings, targets) -> list:
    rows = compute.inventory(tenant, target_ids=targets) if targets else []
    devices = {}
    for row in rows:
        if (not isinstance(row, dict) or row.get("device_id") not in targets
                or row["device_id"] in devices):
            raise HubAccessError("placement_unavailable", "invalid placement inventory", 503)
        devices[row["device_id"]] = row
    result = []
    for device in targets:
        instance = bindings[device]["instance_id"]
        identifier(device, "device_id").encode("utf-8")
        identifier(instance, "instance_id").encode("utf-8")
        row = devices.get(device)
        reasons = placement_reasons(task, row) if row is not None else ["inventory_missing"]
        result.append({"device_id": device, "instance_id": instance,
                       "eligible": not reasons, "reasons": reasons})
    return result


def _result(compute, task, devices, cursor) -> dict:
    timestamp = compute.store.hub.clock()
    try:
        valid_time = type(timestamp) in {int, float} and timestamp >= 0 and math.isfinite(timestamp)
    except OverflowError:
        valid_time = False
    if not valid_time or task["state"] not in STATES:
        raise HubAccessError("placement_unavailable", "placement state is unavailable", 503)
    result = {"task_id": task["task_id"], "state": task["state"],
              "evaluated_at": timestamp, "devices": devices, "next_cursor": cursor}
    if len(json.dumps({"data": result}, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
        raise HubAccessError("placement_too_large", "placement exceeds response limit", 503)
    return result
