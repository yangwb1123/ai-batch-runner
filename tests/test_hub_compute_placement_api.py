"""Durable tasks can be diagnosed without widening grants or mutating execution."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from pbatch.hub_auth import GATEWAY_SCOPE, HubPrincipal
from pbatch.hub_compute_models import COMPUTE_SCOPES
from pbatch.hub_compute_placement import page_query, page_values
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import PREFIX
from pbatch.hub_store import HubStoreError
from test_hub_compute import _delegate, _running_turn, submit, system  # noqa: F401


def placement(system, task, *, user=None, query=None, method="GET", body=None):
    return system.service.dispatch(method, PREFIX + "/tasks/" + task["task_id"] + "/placement",
                                   query or {}, body or {}, user or system.user)[1]["data"]


def track_inventory(system):
    calls, original = [], system.fabric.inventory

    def inventory(ids):
        calls.append(list(ids))
        return original(ids)

    system.fabric.inventory = inventory
    return calls


def snapshot_history_and_state(system):
    conn = system.hub._connect()
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_diagnostics_are_read_only_minimal_and_change_with_live_resource_facts(system):
    task = submit(system)
    system.fabric.online = False
    before = snapshot_history_and_state(system)
    calls = track_inventory(system)
    result = placement(system, task)
    assert set(result) == {"task_id", "state", "evaluated_at", "devices", "next_cursor"}
    assert result["task_id"] == task["task_id"] and result["state"] == "queued"
    assert result["devices"] == [{"device_id": "device-b", "instance_id": "remote",
                                   "eligible": False, "reasons": ["device_offline"]}]
    assert result["next_cursor"] == "" and result["evaluated_at"] >= 0
    assert calls == [["device-b"]] and not system.fabric.requests
    assert snapshot_history_and_state(system) == before
    system.fabric.online = True
    assert placement(system, task)["devices"][0]["eligible"]
    assert snapshot_history_and_state(system) == before
    system.compute.tick()
    assert system.compute.store.get("team", task["task_id"])["state"] == "running"


def test_page_intersects_original_targets_current_grants_and_current_project_before_fabric(system):
    broad = replace(system.user, compute_instances=frozenset({"source", "remote"}))
    task = submit(system, user=broad)
    bindings = system.connection.bindings
    bindings["device-new"] = deepcopy(bindings["device-b"])
    bindings["device-a"]["workspaces"] = {"other-project": "/private"}
    calls = track_inventory(system)
    result = placement(system, task, user=broad)
    assert [row["device_id"] for row in result["devices"]] == ["device-b"]
    assert calls == [["device-b"]] and result["next_cursor"] == ""
    narrower = replace(broad, compute_instances=frozenset({"source"}))
    assert placement(system, task, user=narrower)["devices"] == []
    assert calls == [["device-b"]]
    assert "device-new" not in json.dumps(result) and "private" not in json.dumps(result)


def test_pagination_limits_configured_ids_and_keeps_missing_inventory_rows(system):
    system.connection.bindings = {f"worker-{index:02}": {
        "instance_id": "remote", "workspaces": {"project": "/work"}} for index in range(23)}
    task = submit(system)
    calls, original = [], system.fabric.inventory

    def missing(ids):
        calls.append(list(ids))
        result = original(ids)
        result["devices"] = [row for row in result["devices"] if row["device_id"] != "worker-03"]
        return result

    system.fabric.inventory = missing
    first = placement(system, task)
    assert len(first["devices"]) == 20 and first["next_cursor"] == "worker-19"
    assert [len(page) for page in calls] == [16, 4]
    assert first["devices"][3] == {"device_id": "worker-03", "instance_id": "remote",
                                    "eligible": False, "reasons": ["inventory_missing"]}
    final = placement(system, task, query={"after": first["next_cursor"]})
    assert [row["device_id"] for row in final["devices"]] == ["worker-20", "worker-21", "worker-22"]
    assert final["next_cursor"] == ""
    assert placement(system, task, query={"after": "worker-99"})["devices"] == []
    assert calls[-1] == ["worker-20", "worker-21", "worker-22"]


@pytest.mark.parametrize("state", ["dispatching", "running", "cancel_requested", "completed",
                                   "failed", "cancelled", "lost"])
def test_nonqueued_tasks_never_query_fabric_or_diagnose_their_own_reservations(system, state):
    task = submit(system)
    system.compute.store.update("team", task["task_id"], {}, state=state)
    calls = track_inventory(system)
    result = placement(system, task)
    assert result["state"] == state and result["devices"] == [] and result["next_cursor"] == ""
    assert calls == []


def test_state_advance_during_inventory_discards_stale_observations(system):
    task = submit(system)
    original = system.fabric.inventory

    def advance(ids):
        result = original(ids)
        system.compute.store.cancel("team", task["task_id"])
        return result

    system.fabric.inventory = advance
    result = placement(system, task)
    assert result["state"] == "cancelled" and result["devices"] == [] and result["next_cursor"] == ""


@pytest.mark.parametrize("missing", ["agent.tasks:read", "agent.devices:read"])
def test_required_scopes_are_checked_before_private_task_read(system, missing, monkeypatch):
    task = submit(system)
    user = replace(system.user, scopes=system.user.scopes - {missing})
    monkeypatch.setattr(system.compute.store, "get", lambda *_args, **_kwargs: pytest.fail("private read"))
    with pytest.raises(HubAccessError) as rejected:
        placement(system, task, user=user)
    assert rejected.value.code == "insufficient_scope"


def test_source_grant_gateway_tenant_and_membership_never_reach_inventory(system):
    task = submit(system)
    calls = track_inventory(system)
    for user in (replace(system.user, instances=frozenset({"other"})),
                 replace(system.user, kind="gateway")):
        with pytest.raises(HubAccessError):
            placement(system, task, user=user)
    with pytest.raises(HubStoreError) as missing:
        placement(system, task, user=replace(system.user, tenant_id="other"))
    assert missing.value.status == 404

    def denied(_principal):
        raise HubAccessError("identity_ineligible", "membership denied")

    system.service.identity = SimpleNamespace(check=denied)
    with pytest.raises(HubAccessError, match="membership denied"):
        placement(system, task)
    assert calls == []


def test_offline_and_frozen_source_session_do_not_block_read_only_diagnostics(system):
    task = submit(system)
    system.hub.clock = lambda: 4_000_000_000.0
    system.hub.register_instance("team", "source", {"boot_id": "new-boot"})
    assert system.hub.session("team", task["session_id"])["controllable"] is False
    assert placement(system, task)["devices"][0]["eligible"]


@pytest.mark.parametrize("query", [{"limit": value} for value in
                                   ["0", "21", "-1", "", "1.5", "true", "١", "２", "9" * 5000]] +
                         [{"after": value} for value in [None, False, 1, " bad", "bad ", "a\0b",
                                                          "a\x7fb", "x" * 257, "\ud800"]] +
                         [{"tenant": "other"}, {"session_id": "a"}])
def test_invalid_page_queries_fail_closed(query):
    with pytest.raises((ValueError, UnicodeError)):
        page_query(query)


@pytest.mark.parametrize("limit", [True, 1.0, "1", None, -1, 0, 21])
def test_delegated_limit_requires_bounded_integer(limit):
    with pytest.raises(ValueError):
        page_values("", limit)


def test_unicode_c1_cursor_and_leading_zero_ascii_limit_are_valid():
    assert page_query({"after": "节点\x80", "limit": "00001"}) == {"after": "节点\x80", "limit": 1}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_placement_route_is_read_only(system, method):
    task = submit(system)
    with pytest.raises(HubAccessError) as rejected:
        placement(system, task, method=method)
    assert rejected.value.status == 405


def test_get_body_is_rejected_by_transport_independent_service(system):
    with pytest.raises(ValueError):
        placement(system, submit(system), body={"reserve": True})


@pytest.mark.parametrize("invalid", [True, -1, float("nan"), float("inf"), 10**1000])
def test_invalid_evaluation_clock_does_not_escape_into_json(system, invalid):
    task = submit(system)
    system.hub.clock = lambda: invalid
    with pytest.raises(HubAccessError) as rejected:
        placement(system, task)
    assert rejected.value.status == 503


@pytest.mark.parametrize("mutation", ["duplicate", "bad-resource"])
def test_malformed_fabric_observations_do_not_report_eligible(system, mutation):
    task = submit(system)
    original = system.fabric.inventory
    if mutation == "duplicate":
        system.connection.bindings["device-c"] = deepcopy(system.connection.bindings["device-b"])
        task = submit(system, key="two-targets")

    def malformed(ids):
        result = original(ids)
        if mutation == "duplicate":
            result["devices"] = [result["devices"][0], result["devices"][0]]
        else:
            result["devices"][0]["resources"] = []
        return result

    system.fabric.inventory = malformed
    with pytest.raises(HubAccessError) as rejected:
        placement(system, task)
    assert rejected.value.code == "placement_unavailable"


@pytest.mark.parametrize("section,name,value", [("resources", "cpu_cores", float("inf")),
                                                ("dynamic_state", "available_memory_bytes", float("nan"))])
def test_invalid_resource_probe_blocks_diagnosis_and_actual_placement_until_recovery(system, section, name, value):
    task = submit(system)
    original = system.fabric.inventory

    def malformed(ids):
        result = original(ids)
        for row in result["devices"]:
            row[section][name] = value
        return result

    system.fabric.inventory = malformed
    assert not placement(system, task)["devices"][0]["eligible"]
    system.compute.tick()
    assert system.compute.store.get("team", task["task_id"])["state"] == "queued"
    assert not system.fabric.requests
    system.fabric.inventory = original
    private = system.compute.store.get("team", task["task_id"], private=True)
    system.compute._attempt(private)
    assert system.compute.store.get("team", task["task_id"])["state"] == "running"
    assert len(system.fabric.requests) == 1


def delegated(system, turn, task, *, payload=None, key="", gateway=None):
    gateway = gateway or HubPrincipal("team", "gateway", "cli", "gateway", frozenset({"source"}),
                                     COMPUTE_SCOPES | {GATEWAY_SCOPE}, frozenset({"source", "remote"}))
    body = {"instance_id": "source", "generation": 1, "session_id": system.session["session_id"],
            "turn_id": turn, "operation": "placement", "payload": payload or {"task_id": task["task_id"]},
            "key": key}
    return system.service.dispatch("POST", PREFIX + "/gateway/compute", {}, body, gateway)[1]["data"]


def test_delegation_is_bound_to_active_turn_authority_and_has_no_idempotency_action(system):
    turn = _running_turn(system)
    task = _delegate(system, turn, "submit", {"argv": ["true"]})
    assert delegated(system, turn, task)["devices"][0]["device_id"] == "device-b"
    with pytest.raises(ValueError, match="idempotency"):
        delegated(system, turn, task, key="ignored-key")
    with pytest.raises(ValueError):
        delegated(system, turn, task, payload={"task_id": task["task_id"], "reserve": True})
    other = submit(system, key="human")
    with pytest.raises(HubAccessError, match="active turn"):
        delegated(system, turn, other)
    system.hub.cancel_turn("team", turn, "alice")
    with pytest.raises(HubAccessError, match="active running turn"):
        delegated(system, turn, task)


def test_delegated_device_scope_and_gateway_compute_grants_intersect(system):
    limited = replace(system.user, scopes=system.user.scopes - {"agent.devices:read"})
    turn = _running_turn(system, limited)
    task = _delegate(system, turn, "submit", {"argv": ["true"]})
    calls = track_inventory(system)
    with pytest.raises(HubAccessError) as rejected:
        delegated(system, turn, task)
    assert rejected.value.code == "insufficient_scope" and calls == []


def test_delegated_gateway_cannot_expand_original_user_target_grant(system):
    turn = _running_turn(system)
    task = _delegate(system, turn, "submit", {"argv": ["true"]})
    gateway = HubPrincipal("team", "gateway", "cli", "gateway", frozenset({"source"}),
                           COMPUTE_SCOPES | {GATEWAY_SCOPE}, frozenset({"source"}))
    calls = track_inventory(system)
    assert delegated(system, turn, task, gateway=gateway)["devices"] == []
    assert calls == []
