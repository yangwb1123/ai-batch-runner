"""Shared local/HTTP/CLI/terminal metadata with owned synthetic resources, never live repair."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pbatch import task_center, task_feedback_store as store
from pbatch.ops import ops_main
from pbatch.task_center_terminal import TaskCenterApp
from test_task_feedback import source, feedback, drafting, input_file, case
from test_task_center_http import server, request
from test_task_center_terminal import wait_for, text
from test_task_center import agent_row, ledger


def test_shared_terminal_cli_http_feedback_candidates_do_not_read_payloads_or_execute(server, source, monkeypatch, capsys):
    first = drafting(source, input_file(source, case("terminal-one"), "first.json"))
    second = drafting(source, input_file(source, case("terminal-two"), "second.json"))
    ledger(source, [agent_row(status="failed", task_brief="synthetic failure", reason="quoted diagnostic")])
    scope = source["source_id"]
    expected = task_center.snapshot(source_id=scope)
    paths = list(Path(source["data_dir"]).rglob("*")) + list(store.feedback_root().iterdir())
    before = {str(path): path.read_bytes() for path in paths if path.is_file()}
    original = store.read_bytes
    def metadata_only(path, *args, **kwargs):
        assert Path(path).suffix == ".json"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(store,"read_bytes",metadata_only)
    monkeypatch.setattr(store,"_verify_case",lambda *args: pytest.fail("terminal verified a saved body implicitly"))
    from pbatch import task_feedback_integrity, task_preservation
    monkeypatch.setattr(task_feedback_integrity,"verify_copy",lambda *args, **kwargs: pytest.fail("implicit case verification"))
    monkeypatch.setattr(task_preservation,"verify_copy",lambda *args, **kwargs: pytest.fail("implicit preservation verification"))
    import subprocess, urllib.request
    monkeypatch.setattr(subprocess,"Popen",lambda *args, **kwargs: pytest.fail("terminal spawned an agent or command"))
    with request(server,"/api/feedback?source="+scope) as response:
        remote = json.loads(response.read())["feedback"]
    assert ops_main(["feedback","show",first["feedback_id"],"--source",scope]) == 0
    command = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(urllib.request,"urlopen",lambda *args, **kwargs: pytest.fail("terminal performed a network call"))
    async def scenario():
        app = TaskCenterApp(source=scope, interval=60)
        async with app.run_test(size=(120,40)) as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            row = app.model.items["feedback:"+first["feedback_id"]]
            assert row == command == next(value for value in remote if value["feedback_id"] == first["feedback_id"])
            assert row["duplicate_inspection"]["matching_feedback_ids"] == [second["feedback_id"]]
            app.model.select("feedback:"+first["feedback_id"]); app._paint_detail()
            assert "PRIVATE-CASE-BODY" not in text(app)
            assert "副本未实时复核" in text(app,"record-facts")
            task = expected["tasks"][0]
            actual = app.model.items["tasks:"+task["task_id"]]
            assert actual["usage"] == task["usage"] and actual["repair_preview"] == task["repair_preview"]
            assert actual["capabilities"]["control"] is False
            attention = expected["attention"]["items"][0]
            assert app.model.items["attention:"+attention["attention_id"]] == attention
            assert attention["target"] == {"kind": "tasks", "record_id": task["task_id"]}
            app.model.select("tasks:"+task["task_id"]); app._paint_detail()
            assert "费用未追踪" in text(app,"record-facts") and "独立验收未验证" in text(app,"record-facts")
            await pilot.press("ctrl+q")
    asyncio.run(scenario())
    assert {str(path): path.read_bytes() for path in paths if path.is_file()} == before
    assert set(store.feedback_root().iterdir()) == {path for path in paths if path.parent == store.feedback_root()}


def test_empty_terminal_query_creates_no_catalog_locks_or_viewer_state(tmp_path, monkeypatch):
    home = tmp_path / "empty-private-runtime"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME",str(home))
    async def scenario():
        app = TaskCenterApp(source="unregistered-synthetic",interval=60)
        async with app.run_test() as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            assert app.model.items == {} and app.model.snapshot["sources"] == []
            assert "task source not found" in text(app,"diagnostics")
            await pilot.press("ctrl+q")
    asyncio.run(scenario())
    assert not home.exists()
