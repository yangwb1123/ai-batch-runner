"""Owned bounded compute metadata and authorization; no Fabric dispatch or command evaluation."""
from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pbatch.hub_auth import HubPrincipal
from pbatch.hub_client import HubError
from pbatch.hub_compute_store import ComputeStore
from pbatch.hub_compute_task_center import METHOD, project, read_metadata, source_key
from pbatch.hub_compute_task_center_client import admit
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import HubService, PREFIX
from pbatch.hub_store import HubStore
from test_hub_task_center import view as native_view

SCOPES = frozenset({"agent.instances:read", "agent.tasks:read"})


def raw():
    return {"sources": [{"instance_id": "alpha", "generation": 2, "last_seen": 10.0}],
            "tasks": [{"task_id": "compute-" + "a"*32, "session_id": "session-1", "instance_id": "alpha",
                       "generation": 1, "state": "failed", "created_at": 10.0, "updated_at": 11.0}],
            "sources_partial": False, "tasks_partial": False}


def view(limit=100, source=""):
    return project("team", raw(), limit, source)


def seed(store, tenant="team", instance="alpha", count=1, state="failed"):
    store.register_instance(tenant, instance, {"boot_id": "owned"})
    session = store.upsert_session(tenant, instance, 1, {"local_session_id": "same", "name": "PRIVATE-NATIVE-LABEL"})
    with store._write() as conn:
        conn.executemany("INSERT INTO hub_compute_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("compute-" + (tenant + instance).encode().hex().ljust(24,"0")[:24] + format(i,"08x"),
             tenant, "PRIVATE-ACTOR", instance+str(i), "PRIVATE-INVALID-REQUEST-JSON",
             session["session_id"], instance, 1, state, 10, 11, 0, "PRIVATE-INVALID-DATA-JSON")
            for i in range(count)])
    return session


@pytest.fixture
def compute_store(tmp_path):
    store = HubStore(tmp_path/"owned.sqlite", clock=lambda:10)
    ComputeStore(store)  # setup only; the observer never creates schemas or starts a broker
    try:
        yield store
    finally:
        store.close()


def test_compute_projection_preserves_identity_unknowns_and_does_not_invent_session_or_device_evidence():
    value = view()
    assert admit(value) == value and value["scope"]["required_scopes"] == sorted(SCOPES)
    task = value["tasks"][0]
    assert task["kind"] == "hub_compute_task" and task["hub"]["compute_task_id"] == raw()["tasks"][0]["task_id"]
    assert task["hub"]["session_id"] == "session-1" and task["lineage"]["parent_task_id"] is None
    assert task["goal"]["statement"] is None and task["delivery"]["acceptance"] == "untracked"
    assert task["time"]["elapsed_wall_seconds"] is None and task["usage"]["calls"] is None
    assert task["billing"]["provider_requests"]["observed_total"] is None
    assert not any(task["capabilities"].values()) and task["artifacts"] == []
    assert not {"session_frozen","device_id","turn_id"} & set(task["hub"])
    assert value["attention"]["counts_by_reason"] == {"reported_failure":1}
    assert value["sources"][0]["source_id"] != native_view()["sources"][0]["source_id"]
    assert task["task_id"] not in {row["task_id"] for row in native_view()["tasks"]}


@pytest.mark.parametrize("state", ["queued","dispatching","running","cancel_requested","completed","cancelled","lost"])
def test_activity_cancellation_loss_and_completion_do_not_become_fault_or_acceptance(state):
    value = raw(); value["tasks"][0]["state"] = state
    projected = project("team",value,100)
    assert projected["attention"]["items"] == [] and projected["tasks"][0]["delivery"]["acceptance"] == "untracked"
    assert admit(projected) == projected


def test_sql_reads_no_private_columns_payloads_native_sessions_clock_or_network(compute_store,monkeypatch):
    seed(compute_store)
    reads, connect = [], compute_store._connect
    def guarded():
        conn = connect()
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ:
                reads.append((table,column))
                assert table in {"hub_instances","hub_compute_tasks"}
                assert column not in {"data_json","request_json","actor","idempotency_key","metadata_json","retry_at"}
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn
    monkeypatch.setattr(compute_store,"_connect",guarded)
    monkeypatch.setattr(compute_store,"clock",lambda:pytest.fail("no new clock"))
    from pbatch import task_sources, hub_compute
    monkeypatch.setattr(task_sources,"discover_sources",lambda:pytest.fail("no local discovery"))
    monkeypatch.setattr(hub_compute.HubCompute,"tick",lambda *_:pytest.fail("no dispatch"))
    value = project("team",read_metadata(compute_store,"team",("alpha",)),100)
    assert reads and len(value["tasks"]) == 1 and "PRIVATE" not in json.dumps(value)
    assert admit(value) == value


def test_existing_source_grant_and_membership_precede_any_read_or_compute_availability(monkeypatch):
    class Store:
        def _connect(self): pytest.fail("unauthorized read")
    principal = HubPrincipal("team","alice","console","user",frozenset({"alpha"}),SCOPES)
    for scopes in (set(),{"agent.tasks:read"},{"agent.instances:read","agent.sessions:read"}):
        with pytest.raises(HubAccessError,match="scope"):
            HubService(Store()).dispatch("GET",PREFIX+"/compute-task-center",{}, {},replace(principal,scopes=frozenset(scopes)))
    with pytest.raises(HubAccessError) as error:
        HubService(Store()).dispatch("GET",PREFIX+"/compute-task-center",{}, {},principal)
    assert error.value.status == 503
    class Gate:
        def check(self,_): raise HubAccessError("identity_ineligible","membership blocked")
    with pytest.raises(HubAccessError,match="membership"):
        HubService(Store(),object(),identity=Gate()).dispatch("GET",PREFIX+"/compute-task-center",{}, {},principal)
    with pytest.raises(HubAccessError,match="user"):
        HubService(Store(),object()).dispatch("GET",PREFIX+"/compute-task-center",{}, {},replace(principal,kind="gateway"))


def test_compute_destination_permissions_do_not_expand_source_instance_metadata_read(compute_store):
    seed(compute_store,"team","alpha");seed(compute_store,"team","beta");seed(compute_store,"other","alpha")
    principal = HubPrincipal("team","alice","console","user",frozenset({"alpha"}),SCOPES,frozenset({"beta"}))
    service = HubService(compute_store,object())
    value = service.dispatch("GET",PREFIX+"/compute-task-center",{}, {},principal)[1]["data"]
    assert value["observed_task_count"] == 1 and {s["hub"]["instance_id"] for s in value["sources"]} == {"alpha"}
    assert "PRIVATE" not in json.dumps(value)
    for hidden in (source_key("team","beta"),native_view()["sources"][0]["source_id"]):
        assert service.dispatch("GET",PREFIX+"/compute-task-center",{"source":hidden}, {},principal)[1]["data"]["tasks"] == []
    monkey = replace(principal,instances=frozenset(),compute_instances=frozenset({"*"}))
    assert service.dispatch("GET",PREFIX+"/compute-task-center",{}, {},monkey)[1]["data"]["sources"] == []


def test_authorization_before_limit_and_source_window_are_bounded(compute_store):
    for i in range(65):
        compute_store.register_instance("team","node-"+str(i).zfill(2),{"boot_id":"owned"})
    value = read_metadata(compute_store,"team",("node-64",))
    assert len(value["sources"]) == 1 and value["sources_partial"] is False
    value = read_metadata(compute_store,"team",None,source_key("team","node-64"))
    assert value["sources"] == [] and value["sources_partial"] is True
    assert project("team",value,100)["scope"]["omitted_database_records"] is None


def test_attention_covers_all_selected_tasks_before_display_limit_and_reports_omissions(compute_store):
    seed(compute_store,count=514)
    raw_value = read_metadata(compute_store,"team",("alpha",))
    assert len(raw_value["tasks"]) == 512 and raw_value["tasks_partial"]
    full, small = project("team",raw_value,200),project("team",raw_value,1)
    assert full["attention"] == small["attention"]
    assert small["attention"]["observed_items"] == 513 and len(small["attention"]["items"]) == 128
    assert small["omitted_tasks"] == 511 and small["scope"]["omitted_database_records"] is None
    assert admit(small,1) == small


@pytest.mark.parametrize("field,value", [("task_id","compute-short"),("state","x"*33),("state","accepted"),
    ("generation",2),("created_at",12),("session_id","x"*257),("updated_at",float("inf"))])
def test_corrupted_metadata_refuses_without_salvage(compute_store,field,value):
    seed(compute_store)
    with compute_store._write() as conn:
        conn.execute("UPDATE hub_compute_tasks SET "+field+"=?",(value,))
    with pytest.raises((ValueError,TypeError)):
        project("team",read_metadata(compute_store,"team",("alpha",)),100)


@pytest.mark.parametrize("field", ["private_root","private_hub","tokens","calls","control","method","tenant",
    "identity","source","count","scope","required_scopes","native_kind","session_evidence","partial","attention_phase"])
def test_closed_compute_wire_rejects_unknown_private_fabricated_or_cross_profile_fields(field):
    value = copy.deepcopy(view()); task=value["tasks"][0]
    if field=="private_root":value["data_json"]="PRIVATE"
    elif field=="private_hub":task["hub"]["argv"]=["PRIVATE"]
    elif field=="tokens":task["usage"]["total_tokens"]=0
    elif field=="calls":task["usage"]["calls"]=1
    elif field=="control":task["capabilities"]["resume"]=True
    elif field=="method":value["scope"]["method"]="hub-native-task-facts-v1"
    elif field=="tenant":task["hub"]["tenant_id"]="other"
    elif field=="identity":task["hub"]["compute_task_id"]="compute-"+"f"*32
    elif field=="source":task["source_id"]="src-"+"f"*24
    elif field=="count":value["observed_task_count"]=True
    elif field=="scope":value["scope"]["grants_execution"]=True
    elif field=="required_scopes":value["scope"]["required_scopes"]=["agent.sessions:read"]
    elif field=="native_kind":task["kind"]="hub_turn"
    elif field=="session_evidence":task["hub"]["session_frozen"]=False
    elif field=="partial":value["scope"]["partial_window"]=None
    elif field=="attention_phase":value["attention"]["items"][0]["evidence"]["phase"]="hub_recorded_state"
    with pytest.raises(HubError,match="invalid read-only compute"):
        admit(value)
