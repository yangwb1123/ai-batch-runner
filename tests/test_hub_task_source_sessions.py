"""Three-scope metadata identity references, not parent execution/equivalence or local mapping."""
from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import replace

import pytest

from pbatch.hub_auth import HubPrincipal
from pbatch.hub_client import HubError
from pbatch.hub_errors import HubAccessError
from pbatch.hub_service import HubService, PREFIX
from pbatch.hub_task_source_sessions import (METHOD, SCOPES, MATCHED, UNRESOLVED, project, read_metadata)
from pbatch.hub_task_source_sessions_client import admit
from pbatch.hub_compute_task_center import project as compute_project, source_key
from pbatch.hub_task_center_store import source_key as native_source_key
from pbatch.hub_task_center_view import project as native_project
from test_hub_compute_task_center import compute_store, seed, raw as compute_raw


def raw():
    return dict(compute_raw(), sessions=[{"instance_id":"alpha", "session_id":"session-1", "generation":1,
                                         "updated_at":12.0}], sessions_partial=False)


def view(limit=100, source=""):
    return project("team",raw(),limit,source)


def test_explicit_reference_keeps_compute_identity_but_native_pointer_separate_with_no_equivalence():
    value=view();old=compute_project("team",compute_raw(),100)
    assert admit(value)==value
    assert value["tasks"][0]["task_id"]==old["tasks"][0]["task_id"]
    task=value["tasks"][0];ref=task["source_session_reference"]
    assert ref["state"]==MATCHED and ref["target"]["source_id"]!=task["source_id"]
    target=ref["target"]
    native=native_project("team",dict(raw(),sessions=[dict(raw()["sessions"][0],name="",project_id="",status="ready",frozen=False)],turns=[],turns_partial=False),100)
    assert target["task_id"]==native["tasks"][0]["task_id"] and target["source_id"]==native["sources"][0]["source_id"]
    assert task["lineage"]["parent_task_id"] is None and task["goal"]["statement"] is None
    assert task["usage"]["calls"] is task["usage"]["total_tokens"] is None
    assert task["delivery"]["acceptance"]=="untracked" and not any(task["capabilities"].values())
    assert all(ref[key] is False for key in ("grants_execution","parent_execution_verified","goal_binding_verified","deduplication_authority","billing_addition_authority"))
    assert value["scope"]["method"]==METHOD and value["scope"]["required_scopes"]==SCOPES


@pytest.mark.parametrize("change",["generation","session","empty"])
def test_missing_or_nonmatching_recorded_generation_never_invents_a_parent_or_guesses_from_labels(change):
    data=raw()
    if change=="empty":data["sessions"]=[]
    else:data["sessions"][0][change if change=="generation" else "session_id"]=2 if change=="generation" else "other-session"
    value=project("team",data,100);ref=value["tasks"][0]["source_session_reference"]
    assert ref["state"]==UNRESOLVED and ref["target"] is None
    assert value["scope"]["source_session_references"]["unresolved_references"]==1
    assert admit(value)==value


def test_authorization_and_membership_before_any_read_no_controller_initialization():
    class Store:
        def _connect(self):pytest.fail("read before admission")
    principal=HubPrincipal("team","alice","console","user",frozenset({"alpha"}),frozenset(SCOPES))
    path=PREFIX+"/compute-task-center/source-sessions"
    for scope in SCOPES:
        with pytest.raises(HubAccessError,match="scope"):
            HubService(Store()).dispatch("GET",path,{}, {},replace(principal,scopes=principal.scopes-{scope}))
    with pytest.raises(HubAccessError) as error:HubService(Store()).dispatch("GET",path,{}, {},principal)
    assert error.value.status==503
    class Gate:
        def check(self,_):raise HubAccessError("identity_ineligible","membership blocked")
    with pytest.raises(HubAccessError,match="membership"):
        HubService(Store(),object(),identity=Gate()).dispatch("GET",path,{}, {},principal)
    with pytest.raises(HubAccessError,match="user"):
        HubService(Store(),object()).dispatch("GET",path,{}, {},replace(principal,kind="gateway"))


def test_one_query_only_transaction_reads_only_identity_metadata_not_labels_private_json_clocks_or_dispatch(compute_store,monkeypatch):
    seed(compute_store)
    original=compute_store._connect;reads=[];connections=[]
    columns={"hub_instances":{"instance_id","tenant","generation","last_seen"},
             "hub_compute_tasks":{"task_id","session_id","instance_id","tenant","generation","state","created_at","updated_at"},
             "hub_sessions":{"session_id","instance_id","tenant","generation","updated_at"}}
    def guarded():
        conn=original();connections.append(conn)
        def authorize(action,table,column,*_):
            if action==sqlite3.SQLITE_READ:
                reads.append((table,column));assert table in columns and column in columns[table]
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize);return conn
    monkeypatch.setattr(compute_store,"_connect",guarded)
    monkeypatch.setattr(compute_store,"clock",lambda:pytest.fail("new clock"))
    from pbatch import task_sources,hub_compute
    monkeypatch.setattr(task_sources,"discover_sources",lambda:pytest.fail("local discovery"))
    monkeypatch.setattr(hub_compute.HubCompute,"tick",lambda *_:pytest.fail("dispatch"))
    value=project("team",read_metadata(compute_store,"team",("alpha",)),100)
    assert len(connections)==1 and reads and "PRIVATE" not in json.dumps(value)
    assert value["tasks"][0]["source_session_reference"]["state"]==MATCHED
    assert admit(value)==value
    with pytest.raises(sqlite3.ProgrammingError):connections[0].execute("SELECT 1")


def test_tenant_source_admission_not_destination_or_session_title_controls_references(compute_store):
    alpha=seed(compute_store,"team","alpha");beta=seed(compute_store,"team","beta");seed(compute_store,"other","alpha")
    with compute_store._write() as conn:
        conn.execute("UPDATE hub_compute_tasks SET session_id=? WHERE tenant='team' AND instance_id='alpha'",(beta["session_id"],))
    principal=HubPrincipal("team","alice","console","user",frozenset({"alpha"}),frozenset(SCOPES),frozenset({"beta"}))
    value=HubService(compute_store,object()).dispatch("GET",PREFIX+"/compute-task-center/source-sessions",{}, {},principal)[1]["data"]
    assert value["observed_task_count"]==1 and value["tasks"][0]["source_session_reference"]["target"] is None
    assert {s["hub"]["instance_id"] for s in value["sources"]}=={"alpha"}
    assert "PRIVATE" not in json.dumps(value) and admit(value)==value
    assert read_metadata(compute_store,"team",())==dict(sources=[],tasks=[],sessions=[],sources_partial=False,tasks_partial=False,sessions_partial=False)
    for hidden in (source_key("team","beta"),native_source_key("team","alpha")):
        assert read_metadata(compute_store,"team",("alpha",),hidden)["sessions"]==[]


def test_native_window_is_bounded_and_no_targeted_lookup_salvages_an_old_reference(compute_store):
    chosen=seed(compute_store)
    for index in range(513):
        session=compute_store.upsert_session("team","alpha",1,{"local_session_id":"owned-"+str(index)})
        with compute_store._write() as conn:conn.execute("UPDATE hub_sessions SET updated_at=? WHERE tenant='team' AND session_id=?",(100+index,session["session_id"]))
    data=read_metadata(compute_store,"team",("alpha",))
    assert len(data["sessions"])==512 and data["sessions_partial"]
    assert chosen["session_id"] not in {row["session_id"] for row in data["sessions"]}
    value=project("team",data,1)
    assert value["tasks"][0]["source_session_reference"]["state"]==UNRESOLVED
    assert value["scope"]["omitted_database_records"] is None
    assert value["attention"]["counts_by_reason"]=={"reported_failure":1,"source_feed_gap":1}
    assert admit(value,1)==value


def test_reference_counts_cover_candidates_before_display_limit_and_never_deduplicate_shared_sessions(compute_store):
    seed(compute_store,count=514)
    data=read_metadata(compute_store,"team",("alpha",))
    one,full=project("team",data,1),project("team",data,200)
    summary=one["scope"]["source_session_references"]
    assert summary["native_rows_observed"]==1 and summary["task_references_observed"]==summary["matched_recorded_references"]==512
    assert summary==full["scope"]["source_session_references"] and one["attention"]==full["attention"]
    assert len({task["source_session_reference"]["reference_id"] for task in full["tasks"]})==200
    assert len({task["source_session_reference"]["target"]["task_id"] for task in full["tasks"]})==1
    assert summary["billing_addition_authority"] is summary["deduplication_authority"] is False
    assert admit(one,1)==one


def test_empty_authorized_window_and_ambiguous_native_identity_never_become_zero_cost_or_merge():
    data=dict(raw(),sources=[],tasks=[],sessions=[])
    value=project("team",data,100)
    assert value["scope"]["source_session_references"]["native_rows_observed"]==0 and admit(value)==value
    data=raw();data["sessions"].append(dict(data["sessions"][0]))
    with pytest.raises(ValueError,match="ambiguous duplicate"):project("team",data,100)


def test_shared_sql_vm_cap_and_query_only_transaction_refuse_without_any_partial_response(compute_store,monkeypatch):
    import pbatch.hub_task_source_sessions as module
    original=module._sources
    def expensive(conn,*args,**kwargs):
        assert conn.in_transaction and conn.execute("PRAGMA query_only").fetchone()[0]==1
        with pytest.raises(sqlite3.OperationalError,match="readonly"):
            conn.execute("UPDATE hub_instances SET generation=generation")
        conn.execute("WITH RECURSIVE numbers(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM numbers WHERE n<100000000) SELECT sum(n) FROM numbers").fetchone()
        return original(conn,*args,**kwargs)
    monkeypatch.setattr(module,"_sources",expensive)
    with pytest.raises(sqlite3.OperationalError,match="interrupted"):
        read_metadata(compute_store,"team",("alpha",))


def test_source_window_admission_and_filter_do_not_crawl_outside_64_sources(compute_store):
    for index in range(65):compute_store.register_instance("team","node-%02d"%index,{"boot_id":"owned"})
    selected=read_metadata(compute_store,"team",("node-64",))
    assert len(selected["sources"])==1 and not selected["sources_partial"]
    hidden=read_metadata(compute_store,"team",None,source_key("team","node-64"))
    assert hidden["sources"]==hidden["sessions"]==hidden["tasks"]==[] and hidden["sources_partial"]
    value=project("team",hidden,100,source_key("team","node-64"))
    assert admit(value,100,source_key("team","node-64"))==value


@pytest.mark.parametrize("field,value",[("session_id","x"*257),("generation",3),("updated_at",float("inf"))])
def test_bad_selected_native_metadata_refuses_even_when_not_an_execution_evidence_source(field,value):
    data=raw();data["sessions"][0][field]=value
    with pytest.raises((ValueError,TypeError)):project("team",data,100)


@pytest.mark.parametrize("field",["root_body","target_body","target_tenant","target_instance","target_session","target_generation","target_id","target_namespace","reference_id","parent","cost","reference_scope","required_scopes","summary_counts","native_partial","observed_bool","missing_ref","zero_native_count"])
def test_closed_reference_wire_refuses_fabricated_identity_authority_and_counts(field):
    value=copy.deepcopy(view());task=value["tasks"][0];ref=task["source_session_reference"];target=ref["target"];summary=value["scope"]["source_session_references"]
    if field=="root_body":value["request_json"]="PRIVATE"
    elif field=="target_body":target["name"]="PRIVATE"
    elif field.startswith("target_"):
        key={"target_tenant":"tenant_id","target_instance":"instance_id","target_session":"session_id","target_generation":"reported_generation","target_id":"task_id","target_namespace":"namespace"}[field]
        target[key]=2 if key=="reported_generation" else "foreign"
    elif field=="reference_id":ref["reference_id"]="hr-"+"0"*32
    elif field=="parent":ref["parent_execution_verified"]=True
    elif field=="cost":ref["billing_addition_authority"]=True
    elif field=="reference_scope":value["scope"]["method"]="hub-compute-task-facts-v1"
    elif field=="required_scopes":value["scope"]["required_scopes"]=["agent.tasks:read"]
    elif field=="summary_counts":summary["matched_recorded_references"]=0
    elif field=="native_partial":summary["native_window_partial"]=True
    elif field=="observed_bool":value["observed_task_count"]=True
    elif field=="missing_ref":del task["source_session_reference"]
    else:summary["native_rows_observed"]=0
    with pytest.raises(HubError,match="invalid read-only source-session"):admit(value)
