"""Explicit reference profile end-to-end interfaces, auth fences and no silent fallback."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
import threading
from dataclasses import replace
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import pytest

from pbatch.hub_auth import GATEWAY_SCOPE,HubAuth
from pbatch.hub_cli import hub_main
from pbatch.hub_client import HubClient,HubError
from pbatch.hub_compute_store import ComputeStore
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_service import PREFIX
from pbatch.hub_store import HubStore
from pbatch.hub_task_center_api import validate_read_headers
from pbatch.hub_task_center_report import _script,page_html,security_policy
from pbatch.hub_task_center_terminal import HubTaskCenterApp
from pbatch.hub_task_source_sessions import SCOPES,MATCHED,project
from pbatch.hub_task_source_sessions_client import ROUTE
from test_hub_compute_task_center import seed
from test_hub_task_source_sessions import view,raw
from test_hub_http import _grant
from test_hub_session_operations_client import endpoint
from test_hub_task_center_jwt import signed_task_hub
from test_hub_auth import signed
from test_task_center_terminal import wait_for,text


@pytest.fixture
def reference_http(tmp_path):
    store=HubStore(tmp_path/"owned.sqlite",clock=lambda:10);ComputeStore(store)
    for tenant,instance in (("team","alpha"),("team","beta"),("other","alpha")):seed(store,tenant,instance)
    auth=HubAuth({"mode":"development","grants":[
        _grant("reader",instances=("alpha",),scopes=SCOPES),
        _grant("compute",instances=("alpha",),scopes=SCOPES[:2]),
        _grant("native",instances=("alpha",),scopes=(SCOPES[0],SCOPES[2])),
        _grant("gateway",kind="gateway",instances=("alpha",),scopes={GATEWAY_SCOPE})]})
    server=HubHTTPServer(("127.0.0.1",0),store,auth,compute=object())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f"http://127.0.0.1:{server.server_port}"
    def call(path=ROUTE,token="reader",method="GET",body=None,headers=None):
        content=None if body is None else json.dumps(body).encode()
        request=Request(origin+PREFIX+path,data=content,method=method,
                        headers={"Authorization":"Bearer "+token,"Content-Type":"application/json",**(headers or {})})
        try:response=urlopen(request,timeout=5)
        except HTTPError as error:response=error
        with response:return response.status,response.read(),dict(response.headers)
    try:yield HubClient(origin,"reader"),call,store
    finally:server.shutdown();server.server_close();thread.join(5);store.close()


def test_authenticated_reference_get_is_explicit_requires_all_three_scopes_and_never_uploads_locals(reference_http):
    client,call,_store=reference_http
    value=client.compute_source_session_snapshot()
    assert value["tasks"][0]["source_session_reference"]["state"]==MATCHED
    assert value["scope"]["required_scopes"]==SCOPES and "PRIVATE" not in json.dumps(value)
    assert call(token="native")[0]==call(token="compute")[0]==call(token="gateway")[0]==403
    assert call(token="invalid")[0]==401
    assert "source_session_reference" not in client.compute_task_snapshot()["tasks"][0]
    assert "source_session_reference" not in client.task_snapshot()["tasks"][0]


@pytest.mark.parametrize("route",[ROUTE,ROUTE+"/view"])
@pytest.mark.parametrize("method",["POST","PUT","PATCH","DELETE","HEAD"])
def test_reference_routes_have_no_mutation_download_repair_or_followup_execution(reference_http,route,method):
    _client,call,_store=reference_http
    assert call(route,method=method,body={} if method=="POST" else None)[0]==405


@pytest.mark.parametrize("query",["limit=0","limit=201","limit=1&limit=2","source=alpha","source=/etc/passwd", "session_id=other", "include_local=true","include_native=true","retry=true","token=private"])
def test_unsupported_selection_cannot_become_implicit_lookup_or_authority(reference_http,query):
    _client,call,_store=reference_http
    assert call(ROUTE+"?"+query)[0]==400


def test_reference_gets_refuse_body_mutation_headers_and_duplicate_empty_lengths(reference_http):
    _client,call,_store=reference_http
    headers=Message();headers.add_header("Content-Length","0");headers.add_header("Content-Length","0")
    for path in (ROUTE,ROUTE+"/view"):
        with pytest.raises(ValueError,match="body or mutation"):validate_read_headers(PREFIX+path,headers)
        for header in ({"If-Match":"1"},{"Idempotency-Key":"no"}):assert call(path,headers=header)[0]==400
        assert call(path,body={})[0]==400


def test_public_reference_shell_has_no_metadata_authority_credentials_or_extra_endpoint(reference_http,monkeypatch):
    _client,call,store=reference_http
    monkeypatch.setattr(store,"_connect",lambda:pytest.fail("no shell store read"))
    status,body,headers=call(ROUTE+"/view",token="invalid")
    assert status==200 and body.decode()==page_html(compute=True,source_sessions=True)
    assert headers["Content-Security-Policy"]==security_policy(compute=True,source_sessions=True)
    assert security_policy(compute=True,source_sessions=True)!=security_policy(compute=True)
    assert "unsafe-inline" not in headers["Content-Security-Policy"] and "PRIVATE" not in body.decode()
    assert call(ROUTE+"/view?source=alpha")[0]==400


def test_one_client_request_limit_and_no_fallback_after_auth_profile_or_payload_failure(endpoint,monkeypatch):
    client,state=endpoint;state.body={"data":view()};calls=[]
    original=client._request
    def request(*args,**kwargs):calls.append(kwargs);return original(*args,**kwargs)
    monkeypatch.setattr(client,"_request",request)
    assert client.compute_source_session_snapshot()==view()
    assert state.calls[0][1].startswith(PREFIX+ROUTE+"?") and len(state.calls)==1
    assert calls[0]["response_limit"]==2*1024*1024
    monkeypatch.setattr(client,"compute_task_snapshot",lambda *_:pytest.fail("no compute-only fallback"))
    monkeypatch.setattr(client,"task_snapshot",lambda *_:pytest.fail("no native fallback"))
    state.status=403;state.body={"error":{"message":"PRIVATE-SERVER-ERROR","code":"denied"}}
    with pytest.raises(HubError):client.compute_source_session_snapshot()
    assert len(state.calls)==2


def test_credential_context_changes_or_bad_options_cannot_retain_facts_or_discover_targets(monkeypatch):
    client=HubClient("https://hub.example","OWNED-OLD")
    def request(*_a,**_k):client._token="OWNED-NEW";return view()
    monkeypatch.setattr(client,"_request",request)
    with pytest.raises(HubError,match="changed during read"):client.compute_source_session_snapshot()
    monkeypatch.setattr(client,"_auth_context",lambda:pytest.fail("credentials before valid input"))
    with pytest.raises(ValueError):client.compute_source_session_snapshot(source="session-name")


def test_cli_profile_requires_explicit_compute_selection_before_credentials_and_preserves_full_ids(endpoint,monkeypatch,capsys):
    client,state=endpoint;state.body={"data":view()}
    monkeypatch.setattr(HubClient,"from_env",lambda *_a,**_k:pytest.fail("invalid profile before credentials"))
    assert hub_main(["tasks","--source-sessions","--json"])==2
    assert "requires --compute" in capsys.readouterr().err
    monkeypatch.setattr(HubClient,"from_env",lambda *_a,**_k:client)
    assert hub_main(["tasks","--compute","--source-sessions","--json"])==0
    assert json.loads(capsys.readouterr().out)==view()
    assert hub_main(["tasks","--compute","--source-sessions","--id",view()["tasks"][0]["task_id"]])==0
    assert json.loads(capsys.readouterr().out)==view()["tasks"][0]
    assert all(row[1].startswith(PREFIX+ROUTE+"?") for row in state.calls)


def test_real_signed_jwt_current_scope_intersection_source_grants_and_config_prerequisite(signed_task_hub):
    auth,token,store,origin=signed_task_hub;ComputeStore(store);seed(store);seed(store,instance="beta")
    auth.grants=[replace(auth.grants[0],scopes=frozenset(SCOPES))]
    client=HubClient(origin,token(scope=" ".join(SCOPES)))
    with pytest.raises(HubError) as error:client.compute_source_session_snapshot()
    assert error.value.status==503
    server=HubHTTPServer(("127.0.0.1",0),store,auth,compute=object())
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        url=f"http://127.0.0.1:{server.server_port}";client=HubClient(url,token(scope=" ".join(SCOPES)))
        one=client.compute_source_session_snapshot()
        assert {row["hub"]["instance_id"] for row in one["sources"]}=={"alpha"}
        assert all(row["source_session_reference"]["target"]["instance_id"]=="alpha" for row in one["tasks"])
        auth.grants=[replace(auth.grants[0],instances=frozenset({"beta"}))]
        two=client.compute_source_session_snapshot()
        assert two["sources"][0]["hub"]["instance_id"]=="beta" and one["tasks"][0]["task_id"]!=two["tasks"][0]["task_id"]
        auth.grants=[replace(auth.grants[0],scopes=frozenset(SCOPES[:2]))]
        with pytest.raises(HubError) as error:client.compute_source_session_snapshot()
        assert error.value.status==403
    finally:server.shutdown();server.server_close();thread.join(5)


def test_reference_tui_literal_details_one_reader_credential_scope_revocation_and_no_execution(monkeypatch):
    client=HubClient("https://hub.example","OWNED");calls=[]
    def request(method,path,**_kwargs):
        assert method=="GET" and path.startswith(ROUTE+"?");calls.append(path)
        if len(calls)>1:raise HubError("PRIVATE-SERVER-ERROR",status=403)
        return view()
    monkeypatch.setattr(client,"_request",request)
    monkeypatch.setattr(client,"task_snapshot",lambda *_:pytest.fail("no native fallback"))
    async def scenario():
        app=HubTaskCenterApp(client,compute=True,source_sessions=True,task_id=view()["tasks"][0]["task_id"],interval=60)
        async with app.run_test(size=(120,40)) as pilot:
            await wait_for(lambda:app.model.snapshot is not None);await pilot.pause()
            assert "不是执行父子关系" in text(app,"notice") and "parent_execution_verified" in text(app)
            assert "token=未追踪" in text(app,"record-facts")
            app.action_refresh();assert app.model.snapshot is None and not app.model.items
            await wait_for(lambda:not app._view_reading)
            assert "HTTP 403" in text(app) and "PRIVATE" not in text(app)
            await pilot.press("ctrl+q")
        assert app._view_closed and len(calls)==2
    asyncio.run(scenario())


def browser(mode="normal",malformed=None,source="",next_value=None):
    node=shutil.which("node")
    if not node:pytest.skip("Node missing; VM is not actual browser acceptance")
    data={"script":_script(True,True),"mode":mode,"initial":view(),"next":next_value or view(),"source":source,"malformed":malformed or {}}
    result=subprocess.run([node,str(Path(__file__).parent/"fixtures"/"hub_task_center_browser.js")],
                          input=json.dumps(data),text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


def test_delivered_reference_web_profile_displays_literal_pointer_never_native_followup_or_merge():
    result=browser();detail=json.loads(result["beforeClear"]["detail"])
    assert detail["source_session_reference"]==view()["tasks"][0]["source_session_reference"]
    assert detail["total_tokens"] is None and detail["acceptance"]=="untracked"
    assert len(result["calls"])==1 and ROUTE+"?" in result["calls"][0]["path"]
    assert result["calls"][0]["keys"]==["Authorization"] and result["afterClear"]["tasks"]==0
    assert result["storedTokenInput"]==""


@pytest.mark.parametrize("field",["profile","parent","target","private","scopes","counts","missing","namespace"])
def test_reference_web_rejects_wrong_profile_conflicting_identity_body_or_authority_and_clears(field):
    value=copy.deepcopy(view());ref=value["tasks"][0]["source_session_reference"]
    if field=="profile":value["scope"]["method"]="hub-compute-task-facts-v1"
    elif field=="parent":ref["parent_execution_verified"]=True
    elif field=="target":ref["target"]["session_id"]="other"
    elif field=="private":ref["target"]["name"]="PRIVATE"
    elif field=="scopes":value["scope"]["required_scopes"]=SCOPES[:2]
    elif field=="counts":value["scope"]["source_session_references"]["matched_recorded_references"]=0
    elif field=="missing":del value["tasks"][0]["source_session_reference"]
    else:ref["target"]["namespace"]="local-ledger"
    result=browser("malformed",value)
    assert result["cleared"] and result["beforeClear"]["tasks"]==result["beforeClear"]["attention"]==0
    assert "PRIVATE" not in json.dumps(result)


def test_reference_web_old_scope_replies_and_auth_failure_never_republish_old_pointer_or_loop():
    source="src-"+"f"*24
    next_value=project("team",dict(raw(),sources=[],tasks=[],sessions=[]),100,source)
    for mode in ("late","late_error"):
        result=browser(mode,source=source,next_value=next_value)
        assert len(result["calls"])==2 and result["cleared"] and result["beforeClear"]["tasks"]==0
    result=browser("revoked")
    assert len(result["calls"])==2 and result["beforeClear"]["tasks"]==0 and "PRIVATE" not in json.dumps(result)
