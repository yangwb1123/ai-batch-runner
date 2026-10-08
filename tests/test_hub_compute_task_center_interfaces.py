"""Compute task observer across authenticated HTTP/client/CLI/TUI and delivered Web JS."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import pytest

from pbatch.hub_auth import GATEWAY_SCOPE,HubAuth
from pbatch.hub_cli import hub_main
from pbatch.hub_client import HubClient,HubError
from pbatch.hub_compute_store import ComputeStore
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_task_center_api import validate_read_headers
from pbatch.hub_service import PREFIX
from pbatch.hub_store import HubStore
from pbatch.hub_task_center_report import _script,page_html,security_policy
from pbatch.hub_task_center_terminal import HubTaskCenterApp
from test_hub_compute_task_center import SCOPES,raw,seed,view
from test_hub_http import _grant
from test_hub_session_operations_client import endpoint
from test_hub_task_center_jwt import signed_task_hub
from test_hub_auth import signed
from test_task_center_terminal import wait_for,text


@pytest.fixture
def http_compute(tmp_path):
    store=HubStore(tmp_path/"owned.sqlite",clock=lambda:10);ComputeStore(store)
    for tenant,instance in (("team","alpha"),("team","beta"),("other","alpha")):
        seed(store,tenant,instance)
    auth=HubAuth({"mode":"development","grants":[
        _grant("reader",instances=("alpha",),scopes=SCOPES),
        _grant("native",instances=("alpha",),scopes={"agent.instances:read","agent.sessions:read"}),
        _grant("task-only",instances=("alpha",),scopes={"agent.tasks:read"}),
        _grant("gateway",kind="gateway",instances=("alpha",),scopes={GATEWAY_SCOPE})]})
    server=HubHTTPServer(("127.0.0.1",0),store,auth,compute=object())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f"http://127.0.0.1:{server.server_port}"
    def call(path="/compute-task-center",token="reader",method="GET",body=None,headers=None):
        content=None if body is None else json.dumps(body).encode()
        req=Request(origin+PREFIX+path,data=content,method=method,
                    headers={"Authorization":"Bearer "+token,"Content-Type":"application/json",**(headers or {})})
        try:response=urlopen(req,timeout=5)
        except HTTPError as error:response=error
        with response:return response.status,response.read(),dict(response.headers)
    try:yield HubClient(origin,"reader"),call,store
    finally:server.shutdown();server.server_close();thread.join(5);store.close()


def test_compute_read_is_explicit_and_requires_its_own_scope_without_reading_native_labels(http_compute):
    client,call,_store=http_compute
    value=client.compute_task_snapshot()
    assert value["observed_task_count"]==1 and {s["hub"]["instance_id"] for s in value["sources"]}=={"alpha"}
    assert "PRIVATE" not in json.dumps(value)
    assert call(token="native")[0]==call(token="task-only")[0]==call(token="gateway")[0]==403
    assert call(token="invalid")[0]==401
    with pytest.raises(HubError) as error:client.task_snapshot()
    assert error.value.status==403  # no widening native sessions scope


@pytest.mark.parametrize("method",["POST","PUT","PATCH","DELETE","HEAD"])
def test_compute_observer_has_no_cancel_retry_reschedule_or_other_mutation(http_compute,method):
    _client,call,_store=http_compute
    assert call(method=method,body={} if method=="POST" else None)[0]==405


@pytest.mark.parametrize("query",["limit=0","limit=201","limit=1&limit=2","source=/etc/passwd","instance_id=beta",
    "retry=true","cancel=true","reschedule=true","include_native=true","token=private"])
def test_bad_queries_never_become_actions_or_expand_authority(http_compute,query):
    _client,call,_store=http_compute
    assert call("/compute-task-center?"+query)[0]==400


@pytest.mark.parametrize("headers",[{"Idempotency-Key":"no"},{"If-Match":"1"},{"Content-Length":"1"}])
def test_compute_get_rejects_body_and_mutation_headers(http_compute,headers):
    _client,call,_store=http_compute
    assert call(headers=headers)[0]==400


def test_duplicate_zero_content_lengths_refuse_for_both_observers_and_shells():
    from email.message import Message
    headers=Message();headers.add_header("Content-Length","0");headers.add_header("Content-Length","0")
    for route in ("task-center","compute-task-center","task-center/view","compute-task-center/view"):
        with pytest.raises(ValueError,match="body or mutation"):
            validate_read_headers(PREFIX+"/"+route,headers)


def test_compute_shell_is_static_credential_free_and_has_its_exact_csp(http_compute,monkeypatch):
    _client,call,store=http_compute
    monkeypatch.setattr(store,"_connect",lambda:pytest.fail("static shell must not read store"))
    status,body,headers=call("/compute-task-center/view",token="invalid")
    assert status==200 and body.decode()==page_html(compute=True)
    assert headers["Content-Security-Policy"]==security_policy(compute=True)
    assert security_policy(compute=True)!=security_policy() and "unsafe-inline" not in security_policy(compute=True)
    assert "/compute-task-center?" in body.decode() and "localStorage" not in body.decode()
    assert call("/compute-task-center/view?source=alpha")[0]==400


def test_compute_client_reads_one_closed_metadata_endpoint_and_never_falls_back(endpoint,monkeypatch):
    client,state=endpoint;state.body={"data":view()}
    assert client.compute_task_snapshot()==view() and len(state.calls)==1
    assert state.calls[0][0]=="GET" and state.calls[0][1].startswith(PREFIX+"/compute-task-center?")
    state.status=403;state.body={"error":{"code":"denied","message":"PRIVATE-SERVER-ERROR"}}
    monkeypatch.setattr(client,"task_snapshot",lambda *_:pytest.fail("no native fallback"))
    with pytest.raises(HubError):client.compute_task_snapshot()
    assert len(state.calls)==2


def test_compute_credential_fence_and_bad_option_admission_happen_before_fallback(monkeypatch):
    client=HubClient("https://hub.example","OLD-OWNED")
    def request(*_a,**_k):client._token="NEW-OWNED";return view()
    monkeypatch.setattr(client,"_request",request)
    with pytest.raises(HubError,match="changed during read"):client.compute_task_snapshot()
    monkeypatch.setattr(client,"_auth_context",lambda:pytest.fail("no credential read on invalid input"))
    with pytest.raises(ValueError):client.compute_task_snapshot(source="alpha")


def test_cli_compute_option_returns_same_snapshot_attention_and_full_task_identity(endpoint,monkeypatch,capsys):
    client,state=endpoint;state.body={"data":view()}
    monkeypatch.setattr(HubClient,"from_env",lambda *_a,**_k:client)
    assert hub_main(["tasks","--compute","--json"])==0
    assert json.loads(capsys.readouterr().out)==view()
    assert hub_main(["tasks","--compute","--attention","--json"])==0
    assert json.loads(capsys.readouterr().out)==view()["attention"]
    assert hub_main(["tasks","--compute","--id",view()["tasks"][0]["task_id"]])==0
    assert json.loads(capsys.readouterr().out)==view()["tasks"][0]
    assert all(row[1].startswith(PREFIX+"/compute-task-center?") for row in state.calls)


def test_signed_jwt_scope_intersection_still_applies_to_compute_observer(signed_task_hub):
    auth,token,store,origin=signed_task_hub
    ComputeStore(store);seed(store)
    auth.grants=[replace(auth.grants[0],scopes=SCOPES)]
    client=HubClient(origin,token(scope="agent.instances:read agent.tasks:read"))
    # The fixture has no configured compute controller: do not initialize one on a read.
    with pytest.raises(HubError) as error:client.compute_task_snapshot()
    assert error.value.status==503
    client=HubClient(origin,token(scope="agent.instances:read agent.sessions:read"))
    with pytest.raises(HubError) as error:client.compute_task_snapshot()
    assert error.value.status==403
    seed(store,instance="beta")
    server=HubHTTPServer(("127.0.0.1",0),store,auth,compute=object())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        client=HubClient(f"http://127.0.0.1:{server.server_port}",token(scope="agent.instances:read agent.tasks:read"))
        one=client.compute_task_snapshot()
        assert one["observed_task_count"]==1 and one["sources"][0]["hub"]["instance_id"]=="alpha"
        assert "PRIVATE" not in json.dumps(one)
        auth.grants=[replace(auth.grants[0],instances=frozenset({"beta"}))]
        two=client.compute_task_snapshot()
        assert two["sources"][0]["hub"]["instance_id"]=="beta"
        assert one["tasks"][0]["task_id"]!=two["tasks"][0]["task_id"]
    finally:
        server.shutdown();server.server_close();thread.join(5)


def test_compute_tui_is_same_readonly_observer_and_revocation_clears_it(monkeypatch):
    client=HubClient("https://hub.example","OWNED");calls=[]
    def request(method,path,**_kwargs):
        assert method=="GET" and path.startswith("/compute-task-center?");calls.append(path)
        if len(calls)>1:raise HubError("PRIVATE-ERROR",status=403)
        return view()
    monkeypatch.setattr(client,"_request",request)
    async def scenario():
        app=HubTaskCenterApp(client,compute=True,task_id=view()["tasks"][0]["task_id"],interval=60)
        async with app.run_test(size=(120,40)) as pilot:
            await wait_for(lambda:app.model.snapshot is not None);await pilot.pause()
            assert app.model.snapshot==view() and "不合并原生/本地" in text(app,"notice")
            assert "token=未追踪" in text(app,"record-facts")
            app.action_refresh();assert app.model.snapshot is None and not app.model.items
            await wait_for(lambda:not app._view_reading)
            assert "HTTP 403" in text(app) and "PRIVATE" not in text(app)
            await pilot.press("ctrl+q")
        assert app._view_closed and len(calls)==2
    asyncio.run(scenario())


def browser(mode="normal",malformed=None):
    node=shutil.which("node")
    if not node:pytest.skip("Node missing; VM is not actual browser acceptance")
    data={"script":_script(True),"mode":mode,"initial":view(),"next":view(),"malformed":malformed or {}}
    result=subprocess.run([node,str(Path(__file__).parent/"fixtures"/"hub_task_center_browser.js")],
                          input=json.dumps(data),text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


def test_delivered_compute_web_script_only_reads_compute_metadata_and_preserves_unknowns():
    result=browser();detail=json.loads(result["beforeClear"]["detail"])
    assert result["beforeClear"]["tasks"]==result["beforeClear"]["attention"]==1
    assert detail["compute_task_id"]==raw()["tasks"][0]["task_id"] and detail["session_frozen"] is None
    assert detail["total_tokens"] is None and detail["acceptance"]=="untracked"
    assert len(result["calls"])==1 and "/compute-task-center?" in result["calls"][0]["path"]
    assert result["calls"][0]["keys"]==["Authorization"] and result["afterClear"]["tasks"]==0


@pytest.mark.parametrize("field",["argv","result","data_json","native_kind","native_phase","scope"])
def test_compute_web_rejects_private_or_wrong_profile_payloads_and_clears_stale_facts(field):
    value=copy.deepcopy(view())
    if field=="native_kind":value["tasks"][0]["kind"]="hub_turn"
    elif field=="native_phase":value["tasks"][0]["phase"]="hub_recorded_state"
    elif field=="scope":value["scope"]["grants_execution"]=True
    else:value["tasks"][0]["analysis"][field]="PRIVATE"
    result=browser("malformed",value)
    assert result["cleared"] and result["beforeClear"]["tasks"]==result["beforeClear"]["attention"]==0
    assert "PRIVATE" not in json.dumps(result)


def test_compute_tui_one_reader_rejects_late_old_scope_response(monkeypatch):
    from pbatch.hub_compute_task_center import project
    gate=threading.Event();calls=[];source="src-"+"f"*24
    client=HubClient("https://hub.example","OWNED")
    def read(limit,chosen):
        calls.append(chosen)
        if not chosen:
            assert gate.wait(10)
            return view()
        return project("team",dict(raw(),sources=[],tasks=[]),limit,chosen)
    monkeypatch.setattr(client,"compute_task_snapshot",read)
    monkeypatch.setattr(client,"task_snapshot",lambda *_:pytest.fail("no native read"))
    async def scenario():
        app=HubTaskCenterApp(client,compute=True,interval=60)
        try:
            async with app.run_test() as pilot:
                await wait_for(lambda:len(calls)==1)
                for _ in range(20):app.request_refresh()
                for _ in range(20):app.request_refresh(source=source,force=True)
                assert calls==[""] and not app.model.items
                gate.set();await wait_for(lambda:len(calls)==2 and app.model.snapshot is not None)
                assert calls==["",source] and not app.model.items
                assert app.model.snapshot["scope"]["source_filter"]==source
                await pilot.press("ctrl+q")
        finally:gate.set()
    asyncio.run(scenario())


def test_compute_web_auth_revocation_stops_polling_and_has_no_native_cache_fallback():
    result=browser("revoked")
    assert len(result["calls"])==2 and result["beforeClear"]["tasks"]==result["beforeClear"]["attention"]==0
