"""One shared attention projection across CLI, authenticated HTTP, browser script and local terminal."""
from __future__ import annotations

import asyncio
import copy
import json
import urllib.error

import pytest

from pbatch import task_center
from pbatch.ops import ops_main
from pbatch.task_center_terminal import TaskCenterApp
from pbatch.task_center_terminal_view import TerminalView
from test_task_center import source, agent_row, ledger
from test_task_center_http import server, request
from test_task_center_browser import run_browser
from test_task_center_terminal import wait_for, text


def failed_view(source):
    ledger(source, [agent_row(status="failed", task_brief="quoted task; rm -rf never-run")])
    return task_center.snapshot(source_id=source["source_id"], now=1800000000)


def test_cli_attention_and_existing_json_snapshot_share_body_free_facts(source, capsys):
    expected = failed_view(source)["attention"]
    assert ops_main(["tasks", "--attention", "--json", "--source", source["source_id"]]) == 0
    command = json.loads(capsys.readouterr().out)
    assert command == expected
    assert ops_main(["tasks", "--json", "--source", source["source_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["attention"] == expected
    assert ops_main(["tasks", "--attention", "--source", source["source_id"]]) == 0
    output = capsys.readouterr().out
    assert expected["items"][0]["target"]["record_id"] in output
    assert "零项不证明安全" in output and "不是执行/恢复/合并/清场/上传授权" in output
    assert "never-run" not in output


@pytest.mark.parametrize("args", [["--tui"], ["--id", "tc-" + "a" * 32]])
def test_conflicting_attention_selection_refuses_before_snapshot_or_tui(monkeypatch, capsys, args):
    monkeypatch.setattr(task_center, "snapshot", lambda *args: pytest.fail("read before option admission"))
    assert ops_main(["tasks", "--attention", *args]) == 2
    assert "--attention cannot be combined" in capsys.readouterr().out


def test_authenticated_http_reuses_snapshot_attention_without_gaining_mutation_capabilities(server, source):
    expected = failed_view(source)["attention"]
    route = "/api/tasks?source=" + source["source_id"] + "&attention=true&resume=true&cleanup=true&file=/etc/passwd"
    with request(server, route) as response:
        assert response.headers["Cache-Control"] == "no-store"
        remote = json.loads(response.read())["attention"]
    assert remote == expected
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, route, token="")
    assert exc.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/attention/execute", method="POST")
    assert exc.value.code == 403


def test_terminal_attention_category_reads_no_extra_window_and_keeps_full_target_and_unknowns(source):
    view = failed_view(source)
    calls = []
    def loader(limit, scope):
        calls.append((limit, scope))
        return view
    async def scenario():
        from textual.widgets import DataTable, Select
        app = TaskCenterApp(source=source["source_id"], interval=60, loader=loader)
        async with app.run_test(size=(120,40)) as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            app.query_one("#kind-filter", Select).value = "attention"
            await pilot.pause()
            assert app.query_one("#records", DataTable).row_count == 1
            item = view["attention"]["items"][0]
            app.model.select("attention:" + item["attention_id"]); app._paint_detail()
            assert app.model.items[app.model.selected] == item
            assert item["target"]["record_id"] in text(app)
            assert "不是根因" in text(app,"record-facts") and "未追踪" not in item["target"]["record_id"]
            assert calls == [(100, source["source_id"])]
            await pilot.press("ctrl+q")
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["attention_refresh", "attention_failure", "attention_switch", "attention_stale_error"])
def test_browser_clears_attention_on_poll_or_source_change_and_rejects_late_results_errors(source, mode):
    view = failed_view(source)
    marker = "OLD-ATTENTION-FACT"
    view["attention"]["items"][0]["summary"] = marker
    changed = copy.deepcopy(view)
    changed["attention"]["items"] = []
    changed["attention"].update(observed_items=0, omitted_items=0, counts_by_reason={}, counts_by_kind={})
    result = run_browser([view, changed], mode, old_marker=marker, source=source["source_id"])
    assert marker not in result["all"] and result["tags"] == 0
    assert all(value["method"] == "GET" for value in result["requests"])


def test_browser_literal_attention_text_contains_no_controls_or_additional_fetch(source):
    view = failed_view(source)
    quote = '<img src=x onerror="alert(1)"><script>alert(1)</script>; rm -rf quoted'
    view["attention"]["items"][0]["summary"] = quote
    result = run_browser([view], "attention")
    assert quote in result["attention"] and json.loads(result["detail"])["summary"] == quote
    assert result["tags"] == 0 and len(result["requests"]) == 1
    assert "不是不同缺陷/根因数" in result["attention_summary"]
    assert "operator_action" in result["detail"]


def test_browser_selecting_task_cannot_republish_old_attention_detail_on_refresh(source):
    view = failed_view(source)
    result = run_browser([view, view], "attention_select_other")
    assert "task_id" in result["detail"] and "attention_id" not in result["detail"]


def test_terminal_unknown_legacy_attention_is_not_zero_and_unsupported_version_clears_all_facts(source):
    view = failed_view(source)
    model = TerminalView(source["source_id"])
    generation = model.invalidate()
    legacy = copy.deepcopy(view); del legacy["attention"]
    assert model.publish(generation, legacy)
    broken = copy.deepcopy(view); broken["attention"]["schema_version"] = True
    with pytest.raises(ValueError, match="attention projection"):
        model.publish(generation, broken)
    assert model.items == {} and model.snapshot is None
