"""Shared remote task observer: one reader, literal facts and no remote action on viewer exit."""
from __future__ import annotations

import asyncio
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from textual.widgets import DataTable, Input, Static

from pbatch.hub_client import HubClient, HubError
from pbatch.hub_task_center_terminal import HubTaskCenterApp
from pbatch.hub_task_center_view import project
from test_hub_task_center import raw, view
from test_task_center_terminal import wait_for, text


def client_with_reader(monkeypatch, reader):
    client = HubClient("https://hub.example","owned-synthetic-token")
    def request(method,path,**kwargs):
        assert method == "GET" and urlsplit(path).path == "/task-center"
        assert set(kwargs) == {"response_limit"}
        query = parse_qs(urlsplit(path).query)
        return reader(int(query["limit"][0]), query.get("source",[""])[0])
    monkeypatch.setattr(client,"_request",request)
    return client


def test_remote_tui_selects_same_task_attention_identity_and_never_reads_event_bodies(monkeypatch):
    value = view(); calls = []
    def reader(limit,source):
        calls.append((limit,source)); return value
    client = client_with_reader(monkeypatch,reader)
    async def scenario():
        key = value["tasks"][0]["task_id"]
        app = HubTaskCenterApp(client,task_id=key,interval=60)
        async with app.run_test(size=(120,40)) as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            await pilot.pause()
            assert app.model.snapshot == value and key in text(app)
            assert "Hub session/turn" in text(app,"notice") and "[red]quoted" in text(app)
            assert "token=未追踪" in text(app,"record-facts")
            assert value["attention"]["items"][0]["attention_id"] in app.model.items["attention:"+
                value["attention"]["items"][0]["attention_id"]]["attention_id"]
            assert app.query_one("#records",DataTable).row_count == 3
            await pilot.press("q")
        assert app._view_closed and calls == [(100,"")]
    asyncio.run(scenario())


def test_auth_revocation_during_refresh_clears_rows_details_and_private_server_errors(monkeypatch):
    calls = []
    def reader(limit,source):
        calls.append((limit,source))
        if len(calls)>1:
            raise HubError("PRIVATE-SERVER-ERROR",status=403)
        return view()
    client = client_with_reader(monkeypatch,reader)
    async def scenario():
        app = HubTaskCenterApp(client,task_id=view()["tasks"][0]["task_id"],interval=60)
        async with app.run_test() as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            app.action_refresh()
            assert app.model.items == {} and app.model.snapshot is None and "quoted" not in text(app)
            await wait_for(lambda: len(calls)==2 and not app._view_reading)
            assert app.query_one("#records",DataTable).row_count == 0
            assert "HTTP 403" in text(app) and "PRIVATE" not in text(app) and "quoted" not in text(app)
            await pilot.press("ctrl+q")
    asyncio.run(scenario())


@pytest.mark.parametrize("late_error",[False,True])
def test_one_remote_reader_rejects_late_old_scope_results_and_errors(monkeypatch,late_error):
    gate = threading.Event(); calls = []
    second = "src-"+"f"*24
    def reader(limit,source):
        calls.append(source)
        if not source:
            assert gate.wait(10)
            if late_error: raise HubError("OLD-PRIVATE-ERROR",status=503)
            return view()
        empty = raw(); empty.update(sources=[],sessions=[],turns=[])
        return project("team",empty,limit,source)
    client = client_with_reader(monkeypatch,reader)
    async def scenario():
        app = HubTaskCenterApp(client,interval=60)
        try:
            async with app.run_test() as pilot:
                await wait_for(lambda: len(calls)==1)
                for _ in range(20): app.request_refresh()
                for _ in range(20): app.request_refresh(source=second,force=True)
                assert calls == [""] and not app.model.items
                gate.set()
                await wait_for(lambda: len(calls)==2 and app.model.snapshot is not None)
                assert calls == ["",second] and app.model.snapshot["tasks"] == []
                assert "PRIVATE" not in text(app) and "quoted" not in text(app)
                assert app.model.snapshot["scope"]["source_filter"] == second
                await pilot.press("ctrl+q")
        finally:
            gate.set()
    asyncio.run(scenario())


def test_literal_path_looking_source_is_refused_before_any_extra_authenticated_get(monkeypatch):
    calls=[]
    client=client_with_reader(monkeypatch,lambda *_:calls.append(1) or view())
    async def scenario():
        app=HubTaskCenterApp(client,interval=60)
        async with app.run_test() as pilot:
            await wait_for(lambda:app.model.snapshot is not None)
            box=app.query_one("#source-filter",Input); box.focus(); box.value="/etc/passwd"
            await pilot.press("enter")
            await wait_for(lambda:not app._view_reading)
            assert calls==[1] and app.model.snapshot is None and not app.model.items
            assert "complete source ID" in text(app) and "quoted" not in text(app)
            await pilot.press("ctrl+q")
    asyncio.run(scenario())
