"""One bounded authenticated metadata GET, strict projection admission, no body fallback/cache."""
from __future__ import annotations

import json
import re
from collections import Counter
from urllib.parse import urlencode

from .hub_client import HubError
from .hub_task_center_api import options
from .hub_task_center_view import METHOD, UNTRACKED_GROUPS, project, _candidate, _task
from .task_center_terminal_view import records
from .task_attention import attention_view, _item, _TASK_STATES

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_SOURCE_HUB_FIELDS = {"tenant_id", "instance_id", "reported_generation", "last_heartbeat_at", "liveness"}
_TASK_HUB_FIELDS = {"tenant_id", "instance_id", "session_id", "turn_id", "reported_generation",
                    "session_frozen", "project_label"}


def _equal(value, expected):
    if json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) != json.dumps(
            expected, sort_keys=True, ensure_ascii=False, allow_nan=False):
        raise ValueError("conflicting Hub task projection")


def _sources(value, tenant):
    raw = []
    feed_flags = {"sessions_partial": False, "turns_partial": False}
    for source in value["sources"]:
        hub = source["hub"]
        if set(hub) != _SOURCE_HUB_FIELDS or hub["tenant_id"] != tenant or hub["liveness"] != "not_verified":
            raise ValueError("invalid Hub task source")
        flags = {}
        if len(source["coverage"]) != 2:
            raise ValueError("invalid Hub source coverage")
        for feed, kind in zip(source["coverage"], ("sessions", "turns")):
            flag = feed["truncated"]
            if type(flag) is not bool:
                raise ValueError("invalid Hub source coverage")
            _equal(feed, {"feed": "hub_" + kind, "truncated": flag, "incomplete": flag, "malformed": 0, "error": ""})
            flags[kind + "_partial"] = flag
        if raw and flags != feed_flags:
            raise ValueError("conflicting Hub metadata windows")
        feed_flags = flags
        raw.append({"instance_id": hub["instance_id"], "generation": hub["reported_generation"],
                    "last_seen": hub["last_heartbeat_at"]})
    return raw, feed_flags


def _tasks(value, tenant, partial):
    sources = {row["source_id"]: row for row in value["sources"]}
    candidates, expected = [], []
    keys = {row["task_id"] for row in value["tasks"]}
    for row in value["tasks"]:
        hub, kind = row["hub"], row["kind"]
        if kind not in {"hub_session", "hub_turn"}:
            raise ValueError("unsupported Hub task kind")
        fields = _TASK_HUB_FIELDS | ({"registered_at"} if kind == "hub_turn" else set())
        if set(hub) != fields or hub["tenant_id"] != tenant or type(hub["session_frozen"]) is not bool:
            raise ValueError("invalid Hub task metadata")
        if (hub["instance_id"] != sources[row["source_id"]]["hub"]["instance_id"] or
                (kind == "hub_session" and hub["turn_id"] is not None)):
            raise ValueError("conflicting Hub task source/type")
        raw = {"session_id": hub["session_id"], "turn_id": hub["turn_id"], "generation": hub["reported_generation"],
               "name": row["title"], "project_id": hub["project_label"], "status": row["status"],
               "frozen": hub["session_frozen"], "updated_at": row["time"]["last_observed_at"],
               "created_at": hub.get("registered_at")}
        candidate = _candidate(tenant, sources[row["source_id"]], raw, kind, partial)
        candidates.append(candidate)
        expected.append(_task(candidate, keys))
    if candidates != sorted(candidates, key=lambda row: (row[0], row[1]), reverse=True):
        raise ValueError("conflicting Hub task ordering")
    by_key = {row["task_id"]: row for row in expected}
    for row in expected:
        parent = by_key.get(row["lineage"]["parent_task_id"])
        if parent:
            parent["lineage"]["children"].append(row["task_id"])
    _equal(value["tasks"], expected)


def _counts(value, maximum, choices):
    if not isinstance(value, dict) or set(value) - choices:
        raise ValueError("invalid attention count categories")
    if any(type(count) is not int or not 1 <= count <= maximum for count in value.values()):
        raise ValueError("invalid attention count values")
    return sum(value.values())


def _attention_item(item, sources, source_items, partial, tasks, phase="hub_recorded_state"):
    kind, key = item["target"]["kind"], item["target"]["record_id"]
    code, evidence = item["reason_code"], item["evidence"]
    if kind == "sources":
        _equal(item, source_items[item["attention_id"]])
    elif kind == "tasks":
        if (set(evidence) != {"record_ref", "reported_status", "phase", "partial_window"} or
                not re.fullmatch(r"[a-f0-9]{32}", evidence["record_ref"]) or
                evidence["phase"] != phase or evidence["partial_window"] is not partial or
                _TASK_STATES[evidence["reported_status"].lower()][0] != code):
            raise ValueError("invalid Hub attention task reference")
        note = _TASK_STATES[evidence["reported_status"].lower()][1]
        if key in tasks:
            row = tasks[key]
            _equal(evidence, {"record_ref": row["observations"][-1]["record_ref"], "reported_status": row["status"],
                              "phase": row["phase"], "partial_window": row["analysis"]["partial_window"]})
        _equal(item, _item(sources[item["source_id"]], kind, key, code, note, evidence))
    else:
        raise ValueError("untracked metadata must not invent attention")
    return kind, code


def _attention_counts(attention, baseline, maximum):
    observed, omitted = attention["observed_items"], attention["omitted_items"]
    if not 0 <= observed <= maximum or omitted != max(0, observed - 128):
        raise ValueError("invalid Hub attention window")
    reason_codes = {choice[0] for choice in _TASK_STATES.values()} | {"source_feed_gap"}
    if (_counts(attention["counts_by_reason"], maximum, reason_codes) != observed or
            _counts(attention["counts_by_kind"], maximum, {"tasks", "sources"}) != observed):
        raise ValueError("conflicting Hub attention totals")
    if (attention["counts_by_kind"].get("sources", 0) != baseline["counts_by_kind"].get("sources", 0) or
            attention["counts_by_reason"].get("source_feed_gap", 0) != baseline["counts_by_reason"].get("source_feed_gap", 0)):
        raise ValueError("conflicting Hub source gap counts")


def _attention(value, baseline, phase="hub_recorded_state"):
    attention = value["attention"]
    observed, omitted = attention["observed_items"], attention["omitted_items"]
    _attention_counts(attention, baseline["attention"], value["observed_task_count"] + len(value["sources"]))
    sources = {row["source_id"]: row for row in value["sources"]}
    source_items = {row["attention_id"]: row for row in baseline["attention"]["items"]}
    tasks = {row["task_id"]: row for row in value["tasks"]}
    kinds, reasons = Counter(), Counter()
    for item in attention["items"]:
        kind, code = _attention_item(item, sources, source_items, value["scope"]["partial_window"], tasks, phase)
        kinds[kind] += 1
        reasons[code] += 1
    if any(count > attention["counts_by_kind"].get(key, 0) for key, count in kinds.items()) or any(
            count > attention["counts_by_reason"].get(key, 0) for key, count in reasons.items()):
        raise ValueError("conflicting retained attention counts")
    ids = [row["attention_id"] for row in attention["items"]]
    if ids != sorted(ids):
        raise ValueError("invalid Hub attention ordering")
    expected = dict(baseline["attention"], items=attention["items"], observed_items=observed,
                    omitted_items=omitted, counts_by_reason=attention["counts_by_reason"],
                    counts_by_kind=attention["counts_by_kind"])
    expected["coverage"]["task_candidates"] = value["observed_task_count"]
    expected["coverage"]["task_window_partial"] = bool(value["observed_task_count"] and value["scope"]["partial_window"])
    _equal(attention, expected)


def admit(value, limit: int = 100, source: str = "") -> dict:
    """Reject unknown/private fields and incompatible authority instead of salvaging a response."""
    options(limit, source)
    try:
        records(value, source)
        scope = value["scope"]
        partial, tenant = scope["partial_window"], scope["tenant_id"]
        if type(partial) is not bool or not isinstance(tenant, str) or not tenant:
            raise ValueError("invalid Hub task scope")
        raw_sources, flags = _sources(value, tenant)
        if any(flags.values()) and not partial:
            raise ValueError("conflicting Hub partial scope")
        baseline = project(tenant, {"sources": raw_sources, "sessions": [], "turns": [],
                                   "sources_partial": partial, **flags}, limit, source)
        _equal(scope, baseline["scope"])
        _equal(value["sources"], baseline["sources"])
        if set(value) != set(baseline):
            raise ValueError("unsupported Hub snapshot fields")
        for field in ("schema_version", "generated_at", "window_only", "diagnostics", "activity", *UNTRACKED_GROUPS):
            _equal(value[field], baseline[field])
        observed, omitted = value["observed_task_count"], value["omitted_tasks"]
        if (type(observed) is not int or type(omitted) is not int or not 0 <= observed <= 1024 or
                omitted != observed - len(value["tasks"]) or len(value["tasks"]) != min(limit, observed)):
            raise ValueError("conflicting Hub task counts")
        _tasks(value, tenant, partial)
        _equal(value["status_counts"], dict(Counter(row["status"] for row in value["tasks"])))
        _attention(value, baseline)
        return value
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise HubError("Hub returned an invalid read-only task snapshot", code="invalid_response") from None


def read_snapshot(client, limit: int = 100, source: str = "") -> dict:
    options(limit, source)
    context = client._auth_context()
    query = urlencode({"limit": limit, "source": source})
    value = client._request("GET", "/task-center?" + query, response_limit=MAX_RESPONSE_BYTES)
    if client._auth_context() != context:
        raise HubError("Hub task credentials changed during read; no old facts retained", code="auth_context_changed")
    return admit(value, limit, source)
