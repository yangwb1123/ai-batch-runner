"""Membership admission applies equally to direct clients and delegated tools."""

from types import SimpleNamespace

import pytest

from pbatch.hub_auth import GATEWAY_SCOPE, HubPrincipal, USER_SCOPES
from pbatch.hub_compute import HubCompute
from pbatch.hub_compute_models import COMPUTE_SCOPES
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import HubService, PREFIX
from pbatch.hub_store import HubStore


class Gate:
    def __init__(self):
        self.allowed = True
        self.checked = []

    def check(self, principal):
        self.check_actor(principal.tenant_id, principal.subject, principal.client_id)

    def check_actor(self, tenant, subject, client):
        self.checked.append((tenant, subject, client))
        if not self.allowed:
            raise HubAccessError("identity_ineligible", "membership does not permit access")


@pytest.fixture
def system(tmp_path):
    hub = HubStore(tmp_path / "hub.sqlite")
    hub.register_instance("team", "alpha", {"boot_id": "one"})
    session = hub.upsert_session("team", "alpha", 1, {
        "local_session_id": "local", "controllable": True, "project_id": "project"})
    connection = SimpleNamespace(bindings={
        "worker": {"instance_id": "remote", "workspaces": {"project": "/workspace"}}}, archive=None)
    compute = HubCompute(hub, configured={"team": connection})
    user = HubPrincipal("team", "alice", "console", "user", frozenset({"alpha"}),
                        USER_SCOPES, frozenset({"remote"}))
    gateway = HubPrincipal("team", "machine", "gateway-client", "gateway", frozenset({"alpha"}),
                           COMPUTE_SCOPES | {GATEWAY_SCOPE}, frozenset({"remote"}))
    gate = Gate()
    yield SimpleNamespace(hub=hub, service=HubService(hub, compute, identity=gate), gate=gate,
                          user=user, gateway=gateway, compute=compute, session=session)
    hub.close()


def running(system):
    _, response = system.service.dispatch("POST", PREFIX + "/sessions/" + system.session["session_id"] +
                                          "/turns", {}, {"prompt": "work"}, system.user, "one")
    turn = response["data"]
    system.hub.append_event("team", "alpha", 1, {"event_id": "started",
        "session_id": system.session["session_id"], "turn_id": turn["turn_id"],
        "kind": "turn.running", "payload": {}})
    return turn


def delegate(system, turn):
    return system.service.dispatch("POST", PREFIX + "/gateway/compute", {}, {
        "instance_id": "alpha", "generation": 1, "session_id": system.session["session_id"],
        "turn_id": turn["turn_id"], "operation": "submit", "payload": {"argv": ["true"]},
        "key": "delegated-task"}, system.gateway)


def test_direct_revoked_member_cannot_read_or_admit_new_prompts(system):
    system.gate.allowed = False
    with pytest.raises(HubAccessError, match="membership"):
        system.service.dispatch("GET", PREFIX + "/sessions", {}, {}, system.user)
    with pytest.raises(HubAccessError, match="membership"):
        running(system)
    assert not system.hub.commands("team", "alpha", 1)


def test_delegated_compute_rechecks_original_human_client_identity(system):
    turn = running(system)
    system.gate.allowed = False
    with pytest.raises(HubAccessError, match="membership"):
        delegate(system, turn)
    assert system.gate.checked[-1] == ("team", "alice", "console")
    assert system.compute.store.list("team", None) == []
    system.gate.allowed = True
    delegate(system, turn)
    assert len(system.compute.store.list("team", None)) == 1


def test_gateway_can_report_existing_work_after_human_membership_revocation(system):
    turn = running(system)
    system.gate.allowed = False
    status, _ = system.service.dispatch("POST", PREFIX + "/gateway/events", {}, {
        "instance_id": "alpha", "generation": 1, "session_id": system.session["session_id"],
        "turn_id": turn["turn_id"], "event_id": "finished", "kind": "turn.completed",
        "payload": {}}, system.gateway)
    assert status == 200
    assert system.hub.turn("team", turn["turn_id"])["status"] == "completed"


def test_compute_cancel_fact_keeps_initiator_and_actual_canceller(system):
    from pbatch.hub_integration_models import lifecycle_fact
    from dataclasses import replace
    _, body = system.service.dispatch("POST", PREFIX + "/sessions/" + system.session["session_id"] +
                                      "/tasks", {}, {"argv": ["true"]}, system.user, "task")
    operator = replace(system.user, subject="operator", client_id="operator-console")
    system.service.dispatch("POST", PREFIX + "/tasks/" + body["data"]["task_id"] +
                            "/cancel", {}, {}, operator)
    conn = system.hub._connect()
    try:
        row = conn.execute("SELECT * FROM hub_events WHERE kind='compute.cancelled'").fetchone()
        fact = lifecycle_fact(conn, row)
    finally:
        conn.close()
    assert fact["actor_subject"] == "alice" and fact["trigger_subject"] == "operator"
