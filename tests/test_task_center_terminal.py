"""Owned headless Textual tests; no provider/Hub/native TUI control or real task repair."""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import DataTable, Input, Select, Static

from pbatch import task_center
from pbatch.ops import ops_main
from pbatch.task_center_terminal import TaskCenterApp
from test_task_center_terminal_view import KINDS, SOURCE, TASK, sample


def text(app, widget="record-detail"):
    return str(app.query_one("#" + widget, Static).content)


async def wait_for(predicate, timeout=4):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "owned terminal fixture did not reach its declared state"
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("kind", KINDS)
def test_headless_table_selection_shows_shared_record_identity_and_literal_content(kind):
    value = sample(kind)
    value[kind][0]["title"] = "[red]quoted; rm -rf /never-run[/red]"
    async def scenario():
        app = TaskCenterApp(loader=lambda limit, source: value, interval=60)
        async with app.run_test(size=(120,40)) as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            await pilot.pause()
            table = app.query_one("#records", DataTable)
            assert table.row_count == 1
            key = next(iter(app.model.items))
            assert all(isinstance(cell, Text) for cell in table.get_row(key))
            assert table.get_row(key)[2].plain == key.partition(":")[2]
            table.focus(); await pilot.press("enter")
            assert json.loads(text(app)) == value[kind][0]
            assert "[red]quoted" in text(app)
            assert not app.use_command_palette
            await pilot.press("ctrl+q")
        assert app._view_closed
    asyncio.run(scenario())


def test_initial_full_task_id_selects_only_in_the_same_bounded_snapshot():
    async def scenario():
        app = TaskCenterApp(task_id=TASK, loader=lambda *_: sample(), interval=60)
        async with app.run_test() as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            await pilot.pause()
            assert json.loads(text(app))["task_id"] == TASK
            app.request_refresh(source="missing-source", force=True)
            assert "task_id" not in text(app) and app.model.selected == ""
            await pilot.press("ctrl+q")
    asyncio.run(scenario())


def test_refresh_clears_old_rows_and_detail_before_withdrawal_result_and_missing_record():
    initial = sample("feedback")
    initial["feedback"][0].update(summary="OLD-SUMMARY", duplicate_inspection={"matching_feedback_ids": ["OLD-PEER"]})
    current = copy.deepcopy(initial); current["feedback"][0].update(state="withdrawn", status="withdrawn")
    gate = threading.Event(); calls = []
    def loader(limit, source):
        calls.append((limit,source))
        if len(calls) > 1:
            assert gate.wait(10)
        return initial if len(calls) == 1 else current
    async def scenario():
        app = TaskCenterApp(loader=loader, interval=60)
        try:
            async with app.run_test() as pilot:
                await wait_for(lambda: app.model.snapshot is not None)
                table = app.query_one("#records", DataTable); table.focus(); await pilot.press("enter")
                assert "OLD-SUMMARY" in text(app)
                app.request_refresh(force=True)
                assert table.row_count == 0 and "OLD-SUMMARY" not in text(app) and "OLD-PEER" not in text(app)
                await wait_for(lambda: len(calls) == 2)
                gate.set(); await wait_for(lambda: app.model.snapshot is not None)
                assert json.loads(text(app))["summary"] is None and "OLD-PEER" not in text(app)
                current["feedback"] = []
                app.action_refresh(); await wait_for(lambda: len(calls) == 3 and not app._view_reading)
                assert "不在本次窗口" in text(app) and table.row_count == 0
                await pilot.press("ctrl+q")
        finally:
            gate.set()
    asyncio.run(scenario())


@pytest.mark.parametrize("late_error", [False, True])
def test_source_switch_and_rapid_refresh_keep_one_reader_and_only_latest_scope(late_error):
    gate = threading.Event(); calls = []; active = [0]; maximum = [0]; guard = threading.Lock()
    second = "src-" + "b" * 24
    def loader(limit, source):
        with guard:
            calls.append((limit,source)); active[0] += 1; maximum[0] = max(maximum[0],active[0])
        try:
            if source == SOURCE:
                assert gate.wait(10)
                if late_error:
                    raise OSError("OLD-SOURCE-ERROR")
                value = sample(); value["tasks"][0]["title"] = "OLD-SOURCE-ROW"; return value
            return sample(source=second, name="second")
        finally:
            with guard:
                active[0] -= 1
    async def scenario():
        app = TaskCenterApp(source=SOURCE, loader=loader, interval=60)
        try:
            async with app.run_test() as pilot:
                await wait_for(lambda: len(calls) == 1)
                for _ in range(20):
                    app.request_refresh()  # polling while busy cannot create even queued reads
                for _ in range(20):
                    app.request_refresh(source=second, force=True)
                assert len(calls) == 1 and app.model.items == {} and app.model.snapshot is None
                gate.set(); await wait_for(lambda: len(calls) == 2 and app.model.snapshot is not None)
                await pilot.pause()
                assert calls == [(100,SOURCE), (100,second)] and maximum[0] == 1
                assert "OLD-SOURCE" not in text(app) and "OLD-SOURCE" not in text(app,"summary")
                assert next(iter(app.model.items.values()))["source_id"] == second
                await pilot.press("ctrl+q")
        finally:
            gate.set()
    asyncio.run(scenario())


def test_latest_read_failure_withholds_previous_rows_and_does_not_write_or_retry():
    calls = []
    def loader(*args):
        calls.append(args)
        if len(calls) == 1:
            return sample()
        raise OSError("synthetic unreadable ledger; token=private_token")
    async def scenario():
        app = TaskCenterApp(loader=loader, interval=60)
        async with app.run_test() as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            app.model.select("tasks:"+TASK); app._paint_detail()
            app.action_refresh(); await wait_for(lambda: len(calls) == 2 and not app._view_reading)
            assert app.model.items == {} and app.query_one("#records",DataTable).row_count == 0
            assert "数据不可用" in text(app) and "task_id" not in text(app)
            assert "private_token" not in text(app)
            await pilot.press("ctrl+q")
        assert len(calls) == 2
    asyncio.run(scenario())


def test_quitting_viewer_does_not_dispatch_cancel_agents_or_schedule_pending_reads():
    gate = threading.Event(); calls = []
    def loader(*args):
        calls.append(args); assert gate.wait(10); return sample()
    async def scenario():
        app = TaskCenterApp(loader=loader, interval=60)
        try:
            async with app.run_test() as pilot:
                await wait_for(lambda: len(calls) == 1)
                app.request_refresh(force=True)
                await pilot.press("ctrl+q")
                gate.set()
            assert app._view_closed and not app._view_queued and app.model.items == {}
            app.request_refresh(force=True)
            assert len(calls) == 1
        finally:
            gate.set()
    asyncio.run(scenario())


def test_category_change_clears_selected_details_without_reading_other_sources():
    value = sample("feedback")
    value["tasks"] = sample()["tasks"]
    calls = []
    def loader(*args):
        calls.append(args); return value
    async def scenario():
        app = TaskCenterApp(loader=loader,interval=60)
        async with app.run_test() as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            app.model.select(next(key for key in app.model.items if key.startswith("feedback:"))); app._paint_detail()
            assert "feedback_id" in text(app)
            app.query_one("#kind-filter",Select).value = "tasks"
            await pilot.pause()
            assert app.query_one("#records",DataTable).row_count == 1
            assert "feedback_id" not in text(app) and text(app,"record-facts") == ""
            assert len(calls) == 1
            await pilot.press("ctrl+q")
    asyncio.run(scenario())


def test_typing_action_letters_in_source_input_does_not_refresh_or_quit():
    calls = []
    def loader(*args):
        calls.append(args); return sample()
    async def scenario():
        app = TaskCenterApp(loader=loader, interval=60)
        async with app.run_test() as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            input = app.query_one("#source-filter", Input)
            input.focus(); await pilot.press("r","q")
            assert input.value == "rq" and len(calls) == 1 and not app._view_closed
            await pilot.press("ctrl+q")
    asyncio.run(scenario())


def test_filter_input_is_a_literal_source_selection_not_path_command_or_provider_input():
    calls = []
    empty = sample(); empty["sources"] = []; empty["tasks"] = []
    def loader(limit, source):
        calls.append((limit,source)); return empty
    async def scenario():
        app = TaskCenterApp(loader=loader, interval=60)
        async with app.run_test() as pilot:
            await wait_for(lambda: not app._view_reading)
            input = app.query_one("#source-filter", Input)
            input.value = "/etc/passwd?execute=true; quoted"
            input.focus(); await pilot.press("enter")
            await wait_for(lambda: len(calls) == 2 and not app._view_reading)
            assert calls[-1] == (100,"/etc/passwd?execute=true; quoted")
            assert app.model.items == {} and app.query_one("#records",DataTable).row_count == 0
            await pilot.press("ctrl+q")
    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(120,40), (50,16)])
def test_window_coverage_unknown_costs_and_non_authority_are_visible_at_supported_test_sizes(size):
    value = sample()
    value["sources"][0]["coverage"] = [{"truncated": True}]
    value.update(observed_task_count=123, omitted_tasks=122, diagnostics=["synthetic partial observation"] * 12)
    value["tasks"][0].update(time={"remaining_seconds": None}, usage={"total_tokens": None},
                           capabilities={"control": False, "cleanup": False})
    async def scenario():
        app = TaskCenterApp(loader=lambda *_: value, interval=60)
        async with app.run_test(size=size) as pilot:
            await wait_for(lambda: app.model.snapshot is not None)
            await pilot.pause()
            assert "遗漏任务 122" in text(app,"summary") and "未显示" in text(app,"diagnostics")
            assert "无执行、清场、上传权" in text(app,"notice")
            app.model.select("tasks:"+TASK); app._paint_detail()
            assert json.loads(text(app))["usage"]["total_tokens"] is None
            await pilot.press("ctrl+q")
    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [["--json"], ["--follow"], ["--limit","0"], ["--limit","201"],
    ["--interval","nan"], ["--interval","inf"], ["--interval","0.5"], ["--id","/etc/passwd"]])
def test_conflicting_or_invalid_cli_options_do_not_read_or_launch_a_tui(monkeypatch, capsys, arguments):
    monkeypatch.setattr(task_center,"snapshot",lambda *args: pytest.fail("invalid query performed a source read"))
    from pbatch import task_center_terminal
    monkeypatch.setattr(task_center_terminal,"run_terminal",lambda *args: pytest.fail("invalid query launched terminal"))
    assert ops_main(["tasks","--tui",*arguments]) == 2
    assert "ops tasks:" in capsys.readouterr().out


def test_cli_launch_preserves_explicit_source_task_limit_and_interval(monkeypatch):
    from pbatch import task_center_terminal
    calls = []
    monkeypatch.setattr(task_center_terminal,"run_terminal",lambda *args: calls.append(args) or 0)
    assert ops_main(["tasks","--tui","--source",SOURCE,"--id",TASK,"--limit","7","--interval","8"]) == 0
    assert calls == [(7,SOURCE,TASK,8.0)]


def test_default_json_query_remains_the_shared_snapshot_not_tui(monkeypatch,capsys):
    value = sample()
    monkeypatch.setattr(task_center,"snapshot",lambda *args: value)
    from pbatch import task_center_terminal
    monkeypatch.setattr(task_center_terminal,"run_terminal",lambda *args: pytest.fail("default JSON query launched terminal"))
    assert ops_main(["tasks","--json"]) == 0
    assert json.loads(capsys.readouterr().out) == value
