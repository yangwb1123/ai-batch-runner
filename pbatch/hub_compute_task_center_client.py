"""One explicit compute metadata read; no native/local/task-body fallback or action."""
from __future__ import annotations

from collections import Counter
from urllib.parse import urlencode

from .hub_client import HubError
from .hub_task_center_api import options
from .hub_task_center_client import MAX_RESPONSE_BYTES, _equal, _attention, _SOURCE_HUB_FIELDS
from .hub_task_center_view import UNTRACKED_GROUPS
from .hub_compute_task_center import PHASE, _candidate, _compute_task, project
from .task_center_terminal_view import records


_HUB_FIELDS = {"tenant_id", "instance_id", "session_id", "compute_task_id", "reported_generation", "registered_at"}


def _sources(value, tenant):
    rows, partial = [], False
    for source in value["sources"]:
        hub = source["hub"]
        if set(hub) != _SOURCE_HUB_FIELDS or hub["tenant_id"] != tenant or len(source["coverage"]) != 1:
            raise ValueError("invalid compute source metadata")
        feed = source["coverage"][0]
        flag = feed["truncated"]
        if type(flag) is not bool or (rows and flag != partial):
            raise ValueError("conflicting compute source window")
        partial = flag
        _equal(feed, {"feed": "hub_compute_tasks", "truncated": partial, "incomplete": partial,
                      "malformed": 0, "error": ""})
        rows.append({"instance_id": hub["instance_id"], "generation": hub["reported_generation"],
                     "last_seen": hub["last_heartbeat_at"]})
    return rows, partial


def _tasks(value, tenant, partial):
    sources = {row["source_id"]: row for row in value["sources"]}
    candidates = []
    for task in value["tasks"]:
        hub = task["hub"]
        if (task["kind"] != "hub_compute_task" or set(hub) != _HUB_FIELDS or hub["tenant_id"] != tenant or
                hub["instance_id"] != sources[task["source_id"]]["hub"]["instance_id"]):
            raise ValueError("invalid compute task scope")
        raw = {"task_id": hub["compute_task_id"], "session_id": hub["session_id"],
               "generation": hub["reported_generation"], "created_at": hub["registered_at"],
               "updated_at": task["time"]["last_observed_at"], "state": task["status"]}
        candidates.append(_candidate(tenant, sources[task["source_id"]], raw, partial))
    if candidates != sorted(candidates, key=lambda row: (row[0], row[1]), reverse=True):
        raise ValueError("invalid compute task ordering")
    _equal(value["tasks"], [_compute_task(row) for row in candidates])


def admit(value, limit=100, source=""):
    options(limit, source)
    try:
        records(value, source)
        scope, observed, omitted = value["scope"], value["observed_task_count"], value["omitted_tasks"]
        tenant, partial = scope["tenant_id"], scope["partial_window"]
        if type(partial) is not bool or not isinstance(tenant, str) or not tenant:
            raise ValueError("invalid compute scope")
        sources, tasks_partial = _sources(value, tenant)
        if tasks_partial and not partial:
            raise ValueError("conflicting compute partial window")
        baseline = project(tenant, {"sources": sources, "tasks": [], "sources_partial": partial,
                                    "tasks_partial": tasks_partial}, limit, source)
        if set(value) != set(baseline):
            raise ValueError("unsupported compute snapshot fields")
        for field in ("scope", "sources", "schema_version", "generated_at", "window_only", "diagnostics", "activity", *UNTRACKED_GROUPS):
            _equal(value[field], baseline[field])
        if (type(observed) is not int or type(omitted) is not int or not 0 <= observed <= 512 or
                omitted != observed - len(value["tasks"]) or len(value["tasks"]) != min(limit, observed)):
            raise ValueError("conflicting compute counts")
        _tasks(value, tenant, partial)
        _equal(value["status_counts"], dict(Counter(row["status"] for row in value["tasks"])))
        _attention(value, baseline, PHASE)
        return value
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise HubError("Hub returned an invalid read-only compute snapshot", code="invalid_response") from None


def read_snapshot(client, limit=100, source=""):
    options(limit, source)
    context = client._auth_context()
    value = client._request("GET", "/compute-task-center?" + urlencode({"limit": limit, "source": source}),
                            response_limit=MAX_RESPONSE_BYTES)
    if client._auth_context() != context:
        raise HubError("Hub compute credentials changed during read; no old facts retained", code="auth_context_changed")
    return admit(value, limit, source)
