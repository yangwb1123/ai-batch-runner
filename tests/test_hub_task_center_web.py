"""Literal Web projection, static credential-free shell and authorization/race regressions."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pbatch.hub_task_center_report import _SCRIPT, page_html, security_policy
from pbatch.hub_task_center_view import project
from test_hub_task_center import raw, view
from test_hub_http import hub, publish, send


def test_public_shell_never_reads_store_or_uses_credential_urls(hub, monkeypatch):
    call,store,_clock=hub
    monkeypatch.setattr(store,"_connect",lambda:pytest.fail("shell is not a data endpoint"))
    status,body,headers=call("/task-center/view",token="not-a-credential",raw=True)
    assert status==200 and body==page_html()
    assert headers["Cache-Control"]=="no-store" and headers["Referrer-Policy"]=="no-referrer"
    assert headers["Content-Security-Policy"]==security_policy() and "'unsafe-inline'" not in security_policy()
    assert "localStorage" not in body and "sessionStorage" not in body and "location.search" not in body
    assert "Bearer '+token" in body and "credentials:'omit'" in body and "redirect:'error'" in body
    assert "PRIVATE" not in body and "type='password'" in body
    for query in ("token=secret","access_token=secret","source=src-"+"a"*24,"limit=1"):
        assert call("/task-center/view?"+query)[0]==400
    assert call("/task-center/view",headers={"Idempotency-Key":"no"})[0]==400
    assert call("/task-center",token="not-a-credential")[0]==401


def run_browser(mode,initial=None,next_value=None,malformed=None,source=""):
    node=shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable; this VM check is not actual-browser acceptance")
    data={"script":_SCRIPT,"mode":mode,"initial":initial or view(),"next":next_value or view(),
          "malformed":malformed or {},"source":source}
    result=subprocess.run([node,str(Path(__file__).parent/"fixtures"/"hub_task_center_browser.js")],
                          input=json.dumps(data),text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


def test_web_uses_exact_shared_ids_literal_text_and_no_extra_detail_requests():
    data=raw();data["sessions"][0]["name"]=data["turns"][0]["name"]="<img src=x onerror=evil()> [red]quoted; rm -rf[/red]"
    value=project("team",data,100)
    result=run_browser("normal",initial=value)
    assert result["beforeClear"]["tasks"]==2 and result["beforeClear"]["attention"]==1
    detail=json.loads(result["beforeClear"]["detail"])
    assert detail["record_id"]==value["tasks"][0]["task_id"] and detail["source_id"]==value["tasks"][0]["source_id"]
    assert "<img" in detail["title"] and "[red]quoted" in detail["title"]
    assert not {"img","script","iframe"}&set(result["created"])
    assert detail["total_tokens"] is None and detail["acceptance"]=="untracked"
    assert len(result["calls"])==1 and all(row["method"]=="GET" for row in result["calls"])
    assert result["calls"][0]["keys"]==["Authorization"] and result["storedTokenInput"]==""
    assert result["afterClear"]["tasks"]==result["afterClear"]["attention"]==0


@pytest.mark.parametrize("mode",["failure","revoked"])
def test_failed_refresh_auth_rejection_and_local_logout_do_not_restore_old_facts(mode):
    result=run_browser(mode)
    assert result["cleared"] and result["beforeClear"]["tasks"]==result["beforeClear"]["attention"]==0
    assert "quoted" not in result["beforeClear"]["detail"] and "PRIVATE" not in json.dumps(result)
    assert len(result["calls"])==2 and result["beforeClear"]["error"]
    assert result["storedTokenInput"]==""


@pytest.mark.parametrize("mode",["source","late","late_error"])
def test_source_switch_clears_rows_and_late_old_results_or_errors_cannot_republish(mode):
    source="src-"+"f"*24
    empty=raw();empty.update(sources=[],sessions=[],turns=[])
    value=project("team",empty,100,source)
    result=run_browser(mode,next_value=value,source=source)
    assert result["cleared"] and result["beforeClear"]["tasks"]==result["beforeClear"]["attention"]==0
    assert "quoted" not in json.dumps(result["beforeClear"]) and "PRIVATE" not in json.dumps(result)
    assert len(result["calls"])==2 and "source="+source in result["calls"][-1]["path"]


def test_credential_scope_change_clears_previous_selection_and_reads_only_the_new_scope():
    data=raw();data["sessions"][0]["name"]=data["turns"][0]["name"]="NEW-SCOPE-LABEL"
    result=run_browser("credential",next_value=project("team",data,100))
    assert result["cleared"] and result["beforeClear"]["tasks"]==2 and len(result["calls"])==2
    assert "quoted" not in result["beforeClear"]["detail"] and result["storedTokenInput"]==""


@pytest.mark.parametrize("field",["private_body","grant","full_id","duplicate","unknown_group","count"])
def test_malformed_or_conflicting_snapshot_refuses_and_clears_cached_display(field):
    value=copy.deepcopy(view())
    if field=="private_body":value["tasks"][0]["analysis"]["body"]="PRIVATE-BODY"
    elif field=="grant":value["attention"]["items"][0]["capabilities"]["resume"]=True
    elif field=="full_id":value["tasks"][0]["task_id"]="short"
    elif field=="duplicate":value["tasks"][1]["task_id"]=value["tasks"][0]["task_id"]
    elif field=="unknown_group":value["goals"]= [{"goal_id":"invented"}]
    elif field=="count":value["attention"]["observed_items"]=True
    result=run_browser("malformed",malformed=value)
    assert result["cleared"] and result["beforeClear"]["tasks"]==result["beforeClear"]["attention"]==0
    assert "PRIVATE" not in json.dumps(result) and "quoted" not in result["beforeClear"]["detail"]
