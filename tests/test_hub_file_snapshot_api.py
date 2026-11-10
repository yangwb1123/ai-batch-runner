"""Current dual-scope authority gates exact snapshot routes and bytes."""

from dataclasses import replace

import pytest

from pbatch.hub_auth import GATEWAY_SCOPE, HubPrincipal
from pbatch.hub_errors import HubAccessError
from pbatch.hub_file_snapshot_store import FileSnapshotStore
from pbatch.hub_service import HubService, PREFIX
from tests.test_hub_file_snapshot_store import snapshot_store, terminal


def principal(*, kind="user", scopes=None, instances=None, tenant="team"):
    default = {GATEWAY_SCOPE} if kind == "gateway" else {"agent.sessions:read", "agent.artifacts:read"}
    return HubPrincipal(tenant, "alice", "web", kind, frozenset(instances or {"alpha"}),
                        frozenset(default if scopes is None else scopes))


def route(body):
    return PREFIX + f'/sessions/{body["session_id"]}/turns/{body["turn_id"]}/file-snapshots'


def test_post_then_explicit_list_and_content(snapshot_store):
    _snapshots, hub, body, _clock = snapshot_store
    terminal(hub, body)
    service = HubService(hub)
    status, value = service.dispatch("POST", PREFIX + "/gateway/file-snapshots", {}, body,
                                     principal(kind="gateway"))
    assert status == 200 and value["data"]["state"] == "ready"
    assert service.dispatch("GET", route(body), {}, {}, principal()) == (200, value)
    status, item = service.dispatch("GET", route(body) + "/" + "a" * 32, {}, {}, principal())
    assert status == 200 and item["data"]["content_b64"] == body["files"][0]["content_b64"]


@pytest.mark.parametrize("scopes", [set(), {"agent.sessions:read"}, {"agent.artifacts:read"}])
def test_both_scopes_are_required_for_list_and_bytes(snapshot_store, scopes):
    _snapshots, hub, body, _clock = snapshot_store
    for suffix in ("", "/" + "a" * 32):
        with pytest.raises(HubAccessError) as caught:
            HubService(hub).dispatch("GET", route(body) + suffix, {}, {}, principal(scopes=scopes))
        assert caught.value.code == "insufficient_scope"


def test_current_instance_and_membership_checked_before_every_read(snapshot_store):
    _snapshots, hub, body, _clock = snapshot_store

    class Identity:
        def check(self, _principal):
            raise HubAccessError("inactive_member", "membership revoked")

    with pytest.raises(HubAccessError, match="membership revoked"):
        HubService(hub, identity=Identity()).dispatch("GET", route(body), {}, {}, principal())
    denied = replace(principal(), instances=frozenset({"beta"}))
    with pytest.raises(HubAccessError, match="instance access denied"):
        HubService(hub).dispatch("GET", route(body), {}, {}, denied)


@pytest.mark.parametrize("query,body", [({"path": "report.txt"}, {}), ({}, {"path": "report.txt"})])
def test_no_query_or_get_body(snapshot_store, query, body):
    _snapshots, hub, upload, _clock = snapshot_store
    with pytest.raises(ValueError):
        HubService(hub).dispatch("GET", route(upload), query, body, principal())


@pytest.mark.parametrize("suffix", ["/report.txt", "/..", "/%2fetc", "/" + "a" * 32 + "/more"])
def test_read_ids_are_opaque_never_paths(snapshot_store, suffix):
    _snapshots, hub, body, _clock = snapshot_store
    with pytest.raises(ValueError):
        HubService(hub).dispatch("GET", route(body) + suffix, {}, {}, principal())


def test_post_refuses_query_and_human_uploader(snapshot_store):
    _snapshots, hub, body, _clock = snapshot_store
    service = HubService(hub)
    with pytest.raises(ValueError):
        service.dispatch("POST", PREFIX + "/gateway/file-snapshots", {"extra": "1"}, body,
                         principal(kind="gateway"))
    with pytest.raises(HubAccessError):
        service.dispatch("POST", PREFIX + "/gateway/file-snapshots", {}, body, principal())


def test_historical_reads_remain_tenant_and_turn_bound(snapshot_store):
    snapshots, hub, body, _clock = snapshot_store
    terminal(hub, body)
    snapshots.put("team", body)
    from pbatch.hub_store import HubStoreError
    for tenant, turn in (("other", body["turn_id"]), ("team", "not-this-turn")):
        with pytest.raises(HubStoreError):
            FileSnapshotStore(hub).read(tenant, body["session_id"], turn)
