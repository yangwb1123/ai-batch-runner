"""Owned Hub metadata isolation/admission tests; no providers, original campaigns or case execution."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from pbatch.hub_auth import HubPrincipal
from pbatch.hub_service import HubService, PREFIX
from pbatch.hub_store import HubStore
from pbatch.hub_errors import HubAccessError
from pbatch.hub_task_center_store import read_metadata, source_key
from pbatch.hub_task_center_view import project
from pbatch.hub_task_center_client import admit
from pbatch.task_center_terminal_view import records
from test_hub_http import hub, publish, send, event


def raw():
    return {"sources": [{"instance_id": "alpha", "generation": 1, "last_seen": 10.0}],
            "sessions": [{"session_id": "session-1", "instance_id": "alpha", "generation": 1,
                          "name": "[red]quoted rm -rf[/red]", "project_id": "declared-project",
                          "status": "idle", "frozen": 0, "updated_at": 10.0}],
            "turns": [{"turn_id": "turn-1", "session_id": "session-1", "instance_id": "alpha",
                       "generation": 1, "name": "[red]quoted rm -rf[/red]", "project_id": "declared-project",
                       "status": "failed", "frozen": 0, "created_at": 10.0, "updated_at": 11.0}],
            "sources_partial": False, "sessions_partial": False, "turns_partial": False}


def view(limit=100, source=""):
    return project("team", raw(), limit, source)


def test_projection_keeps_exact_session_turn_identity_unknown_accounting_and_no_authority():
    value = view()
    assert value["observed_task_count"] == 2 and value["generated_at"] is None
    assert admit(value) == value and len(records(value)) == 3
    turn, session = value["tasks"]
    assert turn["kind"] == "hub_turn" and session["kind"] == "hub_session"
    assert turn["lineage"]["parent_task_id"] == session["task_id"]
    assert session["lineage"]["children"] == [turn["task_id"]]
    assert "[red]" in turn["title"] and turn["goal"]["statement"] is None
    for task in value["tasks"]:
        assert task["usage"]["total_tokens"] is None and task["usage"]["calls"] is None
        assert task["billing"]["provider_requests"]["observed_total"] is None
        assert task["time"]["elapsed_wall_seconds"] is None and task["goal"]["acceptance"] == "untracked"
        assert not any(task["capabilities"].values()) and task["artifacts"] == []
    assert value["attention"]["counts_by_reason"] == {"reported_failure": 1}
    assert all(state == "untracked_not_imported" for state in value["scope"]["metadata_groups"].values())


def test_task_limit_never_changes_attention_or_invents_missing_parent_completion():
    full, limited = view(), view(1)
    assert limited["attention"] == full["attention"]
    assert limited["observed_task_count"] == 2 and limited["omitted_tasks"] == 1
    assert limited["tasks"][0]["lineage"]["resolution"] == "unavailable_in_window"
    assert admit(limited, 1) == limited


def test_same_local_labels_across_tenants_and_instances_do_not_merge():
    value = raw()
    other = raw()
    other["sources"][0]["instance_id"] = "beta"
    for field in ("sessions", "turns"):
        other[field][0]["instance_id"] = "beta"
    one, two, three = project("team", value, 100), project("team", other, 100), project("other", value, 100)
    assert len({row["source_id"] for snapshot in (one,two,three) for row in snapshot["sources"]}) == 3
    assert len({row["task_id"] for snapshot in (one,two,three) for row in snapshot["tasks"]}) == 6
    assert len({snapshot["attention"]["items"][0]["attention_id"] for snapshot in (one,two,three)}) == 3


def test_http_uses_existing_user_scope_and_filters_before_window_limits(hub):
    call, _store, _clock = hub
    alpha, ga = publish(call)
    beta, gb = publish(call, "beta")
    for sid, generation, instance, key in ((alpha,ga,"alpha","a"),(beta,gb,"beta","b")):
        turn = send(call, sid, prompt="PRIVATE-USER-PROMPT", key=key)[1]["data"]["turn_id"]
        event(call, sid, turn, generation, kind="turn.failed", event_id=key,
              instance=instance, payload={"text": "PRIVATE-ASSISTANT-BODY"})
    status, result, headers = call("/task-center?limit=1", token="alpha-reader")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    value = result["data"]
    assert len(value["sources"]) == 1 and value["sources"][0]["hub"]["instance_id"] == "alpha"
    assert value["observed_task_count"] == 2 and value["attention"]["observed_items"] == 1
    assert not any(term in json.dumps(value) for term in ("PRIVATE-", beta))
    assert call("/task-center", token="outsider")[1]["data"]["observed_task_count"] == 0
    assert call("/task-center", token="alpha")[0] == 403
    assert call("/task-center", token="invalid")[0] == 401
    hidden_source = source_key("team", "beta")
    assert call("/task-center?source=" + hidden_source, token="alpha-reader")[1]["data"]["sources"] == []
    assert "beta" not in json.dumps(call("/task-center?source=" + hidden_source, token="alpha-reader")[1])


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=nan", "limit=1&limit=2",
    "source=/etc/passwd", "source=https://evil.test", "instance_id=beta", "tenant_id=other",
    "resume=true", "cleanup=true", "token=private", "after=all", "upload=true"])
def test_http_queries_never_expand_read_authority_or_become_actions(hub, query):
    call, _store, _clock = hub
    assert call("/task-center?" + query)[0] == 400


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_http_read_route_refuses_all_mutations(hub, method):
    call, store, _clock = hub
    status, _body, _headers = call("/task-center", method=method, body={} if method=="POST" else None,
                                  raw=method=="HEAD")
    assert status == 405 and store.sessions("team") == []


@pytest.mark.parametrize("headers", [{"Idempotency-Key":"never-send"}, {"If-Match":"1"},
                                      {"Content-Length":"1"}, {"Transfer-Encoding":"chunked"}])
def test_get_refuses_mutation_headers_and_request_body(hub, headers):
    call, _store, _clock = hub
    assert call("/task-center", headers=headers)[0] == 400


def test_metadata_read_never_selects_prompts_events_commands_or_clock(tmp_path, monkeypatch):
    store = HubStore(tmp_path/"private.sqlite", clock=lambda: 10)
    try:
        store.register_instance("team", "alpha", {"boot_id":"boot"})
        session = store.upsert_session("team", "alpha", 1, {"local_session_id":"same", "controllable":True})
        store.submit_turn("team", session["session_id"], "actor", "PRIVATE-PROMPT", "key")
        connect, reads = store._connect, []
        def guarded_connect():
            conn = connect()
            def authorize(action, table, column, *_):
                if action == sqlite3.SQLITE_READ:
                    reads.append((table, column))
                    assert table not in {"hub_events", "hub_commands"}
                    assert column not in {"prompt", "payload_json", "canonical_json", "actor", "idempotency_key", "metadata_json"}
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            return conn
        monkeypatch.setattr(store,"_connect",guarded_connect)
        monkeypatch.setattr(store,"clock",lambda: pytest.fail("no live clock"))
        from pbatch import task_sources
        monkeypatch.setattr(task_sources,"discover_sources",lambda: pytest.fail("no local discovery"))
        result = read_metadata(store, "team", ("alpha",))
        assert len(result["turns"]) == 1 and reads
        assert "PRIVATE" not in json.dumps(project("team",result,100))
    finally:
        store.close()


def test_membership_and_scope_rejection_precede_any_database_read(monkeypatch):
    class Store:
        def _connect(self):
            pytest.fail("unauthorized store read")
    class Gate:
        def check(self, _principal):
            raise HubAccessError("identity_ineligible", "membership blocked")
    principal = HubPrincipal("team","alice","console","user",frozenset({"alpha"}),frozenset())
    service = HubService(Store())
    with pytest.raises(HubAccessError,match="scope"):
        service.dispatch("GET",PREFIX+"/task-center",{}, {}, principal)
    with pytest.raises(HubAccessError,match="membership"):
        HubService(Store(),identity=Gate()).dispatch("GET",PREFIX+"/task-center",{}, {},
            replace(principal,scopes=frozenset({"agent.sessions:read"})))


def test_source_limit_cannot_hide_an_authorized_instance_behind_unauthorized_instances(tmp_path):
    store = HubStore(tmp_path/"private.sqlite", clock=lambda:10)
    try:
        for number in range(65):
            store.register_instance("team", "node-"+str(number).zfill(2), {"boot_id":"owned"})
        selected = read_metadata(store,"team",("node-64",))
        assert [row["instance_id"] for row in selected["sources"]] == ["node-64"]
        assert selected["sources_partial"] is False
        bounded = read_metadata(store,"team",None)
        assert len(bounded["sources"]) == 64 and bounded["sources_partial"] is True
        outside = read_metadata(store,"team",None,source_key("team","node-64"))
        assert outside["sources"] == [] and outside["sources_partial"] is True
        value = project("team",outside,100,source_key("team","node-64"))
        assert value["scope"]["omitted_database_records"] is None
        assert admit(value,100,source_key("team","node-64")) == value
    finally:
        store.close()


@pytest.mark.parametrize("field,value", [("name","x"*257),("status","x"*33),("frozen",3),("generation",2)])
def test_corrupt_metadata_is_not_truncated_or_salvaged_into_a_valid_snapshot(tmp_path,field,value):
    store = HubStore(tmp_path/"private.sqlite",clock=lambda:10)
    try:
        store.register_instance("team","alpha",{"boot_id":"owned"})
        store.upsert_session("team","alpha",1,{"local_session_id":"same"})
        with store._write() as conn:
            conn.execute("UPDATE hub_sessions SET "+field+"=?",(value,))
        with pytest.raises(ValueError):
            project("team",read_metadata(store,"team",("alpha",)),100)
    finally:
        store.close()


def test_metadata_overflow_remains_bounded_and_discloses_unknown_omitted_totals(tmp_path):
    store = HubStore(tmp_path/"private.sqlite",clock=lambda:10)
    try:
        store.register_instance("team","alpha",{"boot_id":"boot"})
        session = store.upsert_session("team","alpha",1,{"local_session_id":"same"})
        with store._write() as conn:
            conn.executemany("INSERT INTO hub_turns VALUES(?,?,?,?,?,?,?,?,?,?,?)",[
                ("turn-"+str(i),"team",session["session_id"],"alpha",1,"actor","PRIVATE",str(i),"failed",10,11)
                for i in range(514)])
        value = project("team",read_metadata(store,"team",("alpha",)),1)
        assert value["observed_task_count"] == 513 and value["scope"]["omitted_database_records"] is None
        assert value["attention"]["observed_items"] == 513 and len(value["attention"]["items"]) == 128
        assert value["attention"]["coverage"]["task_window_partial"] is True
        assert admit(value,1) == value
    finally:
        store.close()
