"""Closed remote task wire contract, authentication fences and CLI admission without fallback."""
from __future__ import annotations

import copy
import json
from urllib.parse import urlsplit, parse_qs

import pytest

from pbatch.hub_client import HubClient, HubError
from pbatch.hub_cli import hub_main
from pbatch.hub_task_center_client import admit
from test_hub_task_center import view
from test_hub_session_operations_client import endpoint


def test_one_metadata_get_reuses_shared_facts_and_no_event_prompt_reads(endpoint):
    client, state = endpoint
    state.body = {"data": view()}
    assert client.task_snapshot() == state.body["data"] and len(state.calls) == 1
    method, path, headers = state.calls[0]
    assert method == "GET" and urlsplit(path).path == "/api/v1/agent/task-center"
    assert parse_qs(urlsplit(path).query) == {"limit":["100"]}
    assert "Content-Length" not in headers and "Idempotency-Key" not in headers
    key = state.body["data"]["sources"][0]["source_id"]
    state.body = {"data": view(source=key)}
    assert client.task_snapshot(source=key) == state.body["data"]
    assert parse_qs(urlsplit(state.calls[-1][1]).query)["source"] == [key]


@pytest.mark.parametrize("limit,source", [(0,""),(201,""),(True,""),("1",""),
    (1,"alpha"),(1,"/etc/passwd"),(1,"https://evil.test"),(1,None),(1,"src-"+"A"*24)])
def test_invalid_options_refuse_before_credentials_or_transport(limit,source,monkeypatch):
    client = HubClient("https://hub.example","token")
    monkeypatch.setattr(client,"_auth_context",lambda:pytest.fail("credential read"))
    monkeypatch.setattr(client,"_request",lambda *_a,**_k:pytest.fail("transport"))
    with pytest.raises(ValueError):
        client.task_snapshot(limit,source)


def alter(value, code):
    if code == "private_root": value["prompt"] = "PRIVATE"
    elif code == "private_task": value["tasks"][0]["prompt"] = "PRIVATE"
    elif code == "private_analysis": value["tasks"][0]["analysis"]["reason"] = "PRIVATE"
    elif code == "private_source": value["sources"][0]["hub"]["private_path"] = "/private"
    elif code == "private_attention": value["attention"]["items"][0]["evidence"]["body"] = "PRIVATE"
    elif code == "tokens": value["tasks"][0]["usage"]["total_tokens"] = 0
    elif code == "task_control": value["tasks"][0]["capabilities"]["control"] = True
    elif code == "scope_control": value["scope"]["grants_execution"] = True
    elif code == "attention_control": value["attention"]["items"][0]["capabilities"]["resume"] = True
    elif code == "identity": value["tasks"][0]["hub"]["session_id"] = "other-session"
    elif code == "foreign_source": value["tasks"][0]["source_id"] = "src-"+"f"*24
    elif code == "foreign_tenant": value["tasks"][0]["hub"]["tenant_id"] = "other"
    elif code == "duplicate": value["tasks"].append(copy.deepcopy(value["tasks"][0]))
    elif code == "root_schema": value["schema_version"] = True
    elif code == "wrong_method": value["scope"]["method"] = "assumed-local-history"
    elif code == "count": value["omitted_tasks"] = -1
    elif code == "order": value["tasks"].reverse()
    elif code == "reason_count": value["attention"]["counts_by_reason"]["reported_failure"] = 2
    elif code == "reason_kind": value["attention"]["counts_by_kind"] = {"goals":1}
    elif code == "reason_rootcause": value["attention"]["items"][0]["root_cause"] = "verified"
    elif code == "untracked_goal": value["goals"] = [{"goal_id":"g-"+"f"*32}]
    elif code == "unsupported_kind": value["tasks"][0]["kind"] = "provider_request"
    elif code == "nonfinite": value["sources"][0]["hub"]["last_heartbeat_at"] = float("nan")
    elif code == "unsafe_integer": value["tasks"][0]["hub"]["reported_generation"] = True
    else: raise AssertionError(code)
    return value


@pytest.mark.parametrize("code", ["private_root","private_task","private_analysis","private_source",
    "private_attention","tokens","task_control","scope_control","attention_control","identity",
    "foreign_source","foreign_tenant","duplicate","root_schema","wrong_method","count","order",
    "reason_count","reason_kind","reason_rootcause","untracked_goal","unsupported_kind","nonfinite","unsafe_integer"])
def test_closed_wire_refuses_private_fields_conflicting_facts_and_fabricated_authority(code):
    with pytest.raises(HubError,match="invalid read-only task") as error:
        admit(alter(view(),code))
    assert error.value.code == "invalid_response" and "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("status", [401,403,404,503])
def test_remote_failure_does_not_fallback_to_local_disk_or_other_endpoints(endpoint,status,monkeypatch):
    client,state = endpoint
    state.status = status
    state.body = {"error":{"code":"denied","message":"PRIVATE-SERVER-ERROR"}}
    from pbatch import task_center, task_sources
    monkeypatch.setattr(task_center,"snapshot",lambda *_:pytest.fail("local fallback"))
    monkeypatch.setattr(task_sources,"discover_sources",lambda:pytest.fail("local discovery"))
    with pytest.raises(HubError):
        client.task_snapshot()
    assert len(state.calls) == 1 and "/task-center" in state.calls[0][1]


def test_credentials_rotation_during_response_refuses_old_scope(monkeypatch):
    client = HubClient("https://hub.example","OWNED-OLD-CREDENTIAL")
    calls = []
    def request(*args,**kwargs):
        calls.append((args,kwargs)); client._token = "OWNED-NEW-CREDENTIAL"; return view()
    monkeypatch.setattr(client,"_request",request)
    with pytest.raises(HubError,match="changed during read") as error:
        client.task_snapshot()
    assert error.value.code == "auth_context_changed" and len(calls) == 1
    assert "OWNED-OLD-CREDENTIAL" not in str(error.value) and "OWNED-NEW-CREDENTIAL" not in str(error.value)


@pytest.mark.parametrize("args", [["--tui","--json"],["--tui","--follow"],["--tui","--attention"],
    ["--attention","--id","tc-"+"a"*32],["--interval","nan"],["--interval","0.5"],
    ["--limit","0"],["--source","alpha"],["--id","/etc/passwd"]])
def test_cli_invalid_combinations_read_no_credentials_and_send_no_request(args,monkeypatch,capsys):
    monkeypatch.setattr(HubClient,"from_env",lambda *_a,**_k:pytest.fail("credential read"))
    assert hub_main(["tasks",*args]) == 2
    assert "fleet tasks:" in capsys.readouterr().err


def test_cli_snapshot_attention_and_task_detail_are_the_exact_shared_metadata(endpoint,monkeypatch,capsys):
    client,state = endpoint
    state.body = {"data":view()}
    monkeypatch.setattr(HubClient,"from_env",lambda *_a,**_k:client)
    assert hub_main(["tasks","--json"]) == 0
    assert json.loads(capsys.readouterr().out) == state.body["data"]
    assert hub_main(["tasks","--attention","--json"]) == 0
    assert json.loads(capsys.readouterr().out) == state.body["data"]["attention"]
    key = state.body["data"]["tasks"][0]["task_id"]
    assert hub_main(["tasks","--id",key]) == 0
    assert json.loads(capsys.readouterr().out) == state.body["data"]["tasks"][0]
    assert hub_main(["tasks"]) == 0
    output = capsys.readouterr().out
    assert "CLI 完成观测=未追踪" in output and "[red]quoted" in output
    assert all(method == "GET" for method,_path,_headers in state.calls)


def test_json_follow_remains_jsonl_and_exit_is_not_task_success(endpoint,monkeypatch,capsys):
    client,state = endpoint
    state.body = {"data":view()}
    monkeypatch.setattr(HubClient,"from_env",lambda *_a,**_k:client)
    from pbatch import hub_task_center_cli
    calls = []
    def sleep(interval):
        calls.append(interval)
        if len(calls) == 2: raise KeyboardInterrupt
    monkeypatch.setattr(hub_task_center_cli.time,"sleep",sleep)
    assert hub_main(["tasks","--json","--follow"]) == 0
    values = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert values == [state.body["data"],state.body["data"]] and calls == [5.0,5.0]
