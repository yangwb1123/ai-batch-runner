"""Exact, bounded compute placement observations for user and delegated reads."""

import json
import math
from urllib.parse import quote, urlencode

from .hub_auth_feedback import error_status
from .hub_client import HubError
from .hub_session_client import _identifier
from .hub_session_operations_client import visible

MAX_RESPONSE_BYTES = 64 * 1024
STATES = {"queued", "dispatching", "running", "cancel_requested", "completed",
          "failed", "cancelled", "lost"}
REASONS = {"device_offline", "device_not_ready", "device_busy", "device_unavailable",
           "project_unavailable", "target_not_authorized", "target_mismatch",
           "workspace_unsupported", "cpu_insufficient", "memory_insufficient",
           "os_mismatch", "architecture_mismatch", "runtime_missing", "gpu_unavailable",
           "gpu_insufficient", "inventory_missing"}


def identifier(value):
    _identifier(value)
    value.encode("utf-8")
    return value


def parameters(task_id, after="", limit=20):
    identifier(task_id)
    if after != "":
        identifier(after)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("placement limit must be between 1 and 20")
    return {"task_id": task_id, "after": after, "limit": limit}


def delegated_parameters(payload, key=""):
    if (not isinstance(payload, dict) or "task_id" not in payload
            or set(payload) - {"task_id", "after", "limit"} or key != ""):
        raise ValueError("invalid read-only placement request")
    return parameters(payload["task_id"], payload.get("after", ""), payload.get("limit", 20))


def _row(value):
    if not isinstance(value, dict) or set(value) != {"device_id", "instance_id", "eligible", "reasons"}:
        raise ValueError("invalid placement device fields")
    identifier(value["device_id"])
    identifier(value["instance_id"])
    eligible, reasons = value["eligible"], value["reasons"]
    if (type(eligible) is not bool or not isinstance(reasons, list)
            or any(not isinstance(reason, str) or reason not in REASONS for reason in reasons)
            or len(reasons) != len(set(reasons)) or eligible != (not reasons)):
        raise ValueError("invalid placement reasons")
    return value.copy()


def _device_page(value, after, limit):
    rows, cursor = value["devices"], value["next_cursor"]
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("invalid placement page size")
    records = [_row(row) for row in rows]
    identifiers = [row["device_id"] for row in records]
    if identifiers != sorted(set(identifiers)) or any(item <= after for item in identifiers):
        raise ValueError("invalid placement order")
    if cursor != "" and (identifier(cursor) == after or not records or cursor != identifiers[-1]):
        raise ValueError("invalid placement cursor")
    if value["state"] != "queued" and (records or cursor):
        raise ValueError("unexpected nonqueued placement observations")
    return records


def placement(value, *, task_id, after="", limit=20):
    try:
        if not isinstance(value, dict) or set(value) != {
                "task_id", "state", "evaluated_at", "devices", "next_cursor"}:
            raise ValueError("invalid placement fields")
        if identifier(value["task_id"]) != task_id or value["state"] not in STATES:
            raise ValueError("placement task identity changed")
        stamp = value["evaluated_at"]
        if type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp < 0:
            raise ValueError("invalid evaluation timestamp")
        return dict(value, devices=_device_page(value, after, limit))
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError):
        raise HubError("Hub returned invalid compute placement diagnostics", code="invalid_response") from None


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate placement field")
        result[key] = value
    return result


def decode(raw, **query):
    try:
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("placement response exceeds limit")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
        if not isinstance(value, dict) or set(value) != {"data"}:
            raise ValueError("invalid placement envelope")
        return placement(value["data"], **query)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise HubError("Hub returned invalid compute placement diagnostics", code="invalid_response") from None


def read(client, task_id, after="", limit=20, *, context=None):
    query = parameters(task_id, after, limit)
    auth = client._auth_context()
    options = {"response_limit": MAX_RESPONSE_BYTES, "raw_response": True}
    if context is None:
        path = "/tasks/" + quote(task_id, safe="") + "/placement?" + urlencode({"after": after, "limit": limit})
        raw = client._request("GET", path, **options)
    else:
        raw = client._request("POST", "/gateway/compute",
                              dict(context, operation="placement", payload=query, key=""), **options)
    if auth != client._auth_context():
        raise HubError("Sign-in changed; reload placement diagnostics", code="identity_changed", status=401)
    return decode(raw, **query)


def failure(error):
    if isinstance(error, HubError) and error.status in {404, 405, 501}:
        return "This Hub does not support compute placement diagnostics."
    return error_status("Placement diagnostics unavailable; refresh to retry", error)


def describe(value):
    lines = [f"Task: {visible(value['task_id'])} · {value['state']}"]
    if value["state"] != "queued":
        return "\n".join(lines + ["Placement diagnostics apply only while the task is queued."])
    lines.append("Live observation only; Fabric still decides whether it can accept the task.")
    if not value["devices"]:
        lines.append("No visible original device bindings on this page; hidden capacity is unknown.")
    for row in value["devices"]:
        reason = "eligible" if row["eligible"] else ", ".join(row["reasons"])
        lines.append(f"{visible(row['device_id'])} · {visible(row['instance_id'])} · {reason}")
    if value["next_cursor"]:
        lines.append("Next page: --after " + visible(value["next_cursor"]))
    return "\n".join(lines)
