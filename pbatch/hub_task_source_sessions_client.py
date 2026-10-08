"""One closed metadata response for explicit source-session reference observation only."""
from __future__ import annotations

import copy
from collections import Counter
from urllib.parse import urlencode

from .hub_client import HubError
from .hub_task_center_api import options
from .hub_task_center_client import MAX_RESPONSE_BYTES, _equal, _attention
from .hub_compute_task_center_client import _sources, _tasks
from .hub_compute_task_center import PHASE, project as compute_project
from .hub_task_center_view import UNTRACKED_GROUPS
from .hub_task_source_sessions import (FEED, MATCHED, UNRESOLVED, decorate, reference,
                                      session_index, scope_summary)
from .task_attention import attention_view
from .task_center_terminal_view import records

ROUTE = "/compute-task-center/source-sessions"


def _summary(value):
    summary = value["scope"]["source_session_references"]
    native, observed, matched = (summary[key] for key in (
        "native_rows_observed", "task_references_observed", "matched_recorded_references"))
    native_partial, compute_partial = summary["native_window_partial"], summary["compute_window_partial"]
    if (any(type(number) is not int for number in (native, observed, matched, value["observed_task_count"])) or
            (not value["sources"] and native != 0) or not 0 <= native <= 512 or not 0 <= matched <= observed <= 512 or
            type(native_partial) is not bool or type(compute_partial) is not bool or
            (native_partial and native != 512) or (matched and not native) or
            observed != value["observed_task_count"]):
        raise ValueError("invalid source-session reference counts/window")
    _equal(summary, scope_summary(native, native_partial, observed, matched, compute_partial))
    return summary


def _base_sources(value, tenant, summary):
    base = copy.deepcopy(value)
    for source in base["sources"]:
        if len(source["coverage"]) != 2:
            raise ValueError("unsupported source-session reference coverage")
        extra = source["coverage"].pop()
        partial = summary["native_window_partial"]
        _equal(extra, {"feed": FEED, "truncated": partial, "incomplete": partial, "malformed": 0, "error": ""})
    raw, tasks_partial = _sources(base, tenant)
    if tasks_partial and not summary["compute_window_partial"]:
        raise ValueError("conflicting compute source window")
    return base, raw, tasks_partial


def _references(value, tenant, summary):
    sources = {row["hub"]["instance_id"]: row for row in value["sources"]}
    counts, native = Counter(), {}
    for task in value["tasks"]:
        ref, hub = task["source_session_reference"], task["hub"]
        target = ref["target"]
        if target is not None:
            raw = {"session_id": target["session_id"], "instance_id": target["instance_id"],
                   "generation": target["reported_generation"], "updated_at": target["last_recorded_at"]}
            indexed = session_index(tenant, [raw], sources)
            key = hub["instance_id"], hub["session_id"], hub["reported_generation"]
            if key not in indexed:
                raise ValueError("source-session target is not the compute record's exact reference")
            _equal(target, indexed[key])
            if key in native:
                _equal(native[key], target)
            native[key] = target
        _equal(ref, reference(tenant, hub, target))
        counts[ref["state"]] += 1
    if (counts[MATCHED] > summary["matched_recorded_references"] or
            counts[UNRESOLVED] > summary["unresolved_references"] or
            len(native) > summary["native_rows_observed"]):
        raise ValueError("conflicting retained source-session reference counts")


def admit(value, limit=100, source=""):
    options(limit, source)
    try:
        records(value, source)
        tenant, partial = value["scope"]["tenant_id"], value["scope"]["partial_window"]
        summary = _summary(value)
        if (type(partial) is not bool or not isinstance(tenant, str) or not tenant or
                partial != (summary["compute_window_partial"] or summary["native_window_partial"])):
            raise ValueError("invalid source-session reference scope")
        base, sources, tasks_partial = _base_sources(value, tenant, summary)
        baseline = compute_project(tenant, {"sources": sources, "tasks": [],
                                   "sources_partial": partial, "tasks_partial": tasks_partial}, limit, source)
        decorate(baseline, summary)
        baseline["attention"] = attention_view(baseline, [])
        if set(value) != set(baseline):
            raise ValueError("unsupported source-session snapshot fields")
        for field in ("scope", "sources", "schema_version", "generated_at", "window_only", "diagnostics", "activity", *UNTRACKED_GROUPS):
            _equal(value[field], baseline[field])
        observed, omitted = value["observed_task_count"], value["omitted_tasks"]
        if (type(omitted) is not int or omitted != observed - len(value["tasks"]) or
                len(value["tasks"]) != min(limit, observed)):
            raise ValueError("conflicting source-session display counts")
        _references(value, tenant, summary)
        for task in base["tasks"]:
            del task["source_session_reference"]
        _tasks(base, tenant, partial)
        _equal(value["status_counts"], dict(Counter(row["status"] for row in value["tasks"])))
        _attention(value, baseline, PHASE)
        return value
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise HubError("Hub returned an invalid read-only source-session reference snapshot", code="invalid_response") from None


def read_snapshot(client, limit=100, source=""):
    options(limit, source)
    context = client._auth_context()
    value = client._request("GET", ROUTE + "?" + urlencode({"limit": limit, "source": source}),
                            response_limit=MAX_RESPONSE_BYTES)
    if client._auth_context() != context:
        raise HubError("Hub reference credentials changed during read; no old facts retained", code="auth_context_changed")
    return admit(value, limit, source)
