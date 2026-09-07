"""Pure projection and headless TUI contracts for ``/agents``."""

from __future__ import annotations

import asyncio
import json
import threading
import time

from textual.widgets import Input, Markdown, OptionList, Static

from pbatch import config
from pbatch.agent_ledger import LedgerQuerySnapshot, record
from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_agent_runs import (
    AgentRunRecord,
    TUIAgentRunsScreen,
    build_agent_runs_snapshot,
)
from pbatch.tui_actor_tree import (
    ActorHistoryGroup, ActorHistorySnapshot, ActorNode, ActorTreeSnapshot,
)
from pbatch.tui_agents import AgentLedgerView, render_breaker_lines
from pbatch.tui_shell import SLASH_HELP


class FakeBackend:
    def __init__(self, emit, **_options):
        self.emit = emit
        self.prompts = []

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        return {"success": True, "stdout": "", "stderr": ""}

    def snapshot(self):
        return {"status": "idle", "session_name": "agents-test",
                "agent": "fake", "provider": "local"}


class BlockingBackend(FakeBackend):
    def __init__(self, emit, **options):
        super().__init__(emit, **options)
        self.started = threading.Event()
        self.release = threading.Event()

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        self.started.set()
        self.release.wait(timeout=5)
        return {"success": True, "stdout": "done", "stderr": ""}

    def snapshot(self):
        return {"status": "running" if self.started.is_set() else "idle",
                "session_name": "agents-test", "session_id": "ses_current",
                "agent": "codex", "provider": "openai"}

    def cancel(self):
        self.release.set()
        return True


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


async def _wait_until(predicate, pilot, attempts=200):
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition did not become true")


def test_view_renders_latest_per_agent_and_truncates_width(tmp_path):
    record(tmp_path, agent="codex", status="failed", task_brief="old", ts="01")
    record(tmp_path, agent="claude", status="succeeded", task_brief="review", ts="02")
    record(tmp_path, agent="codex", status="succeeded",
           task_brief="a very long implementation task", ts="03")
    view = AgentLedgerView()
    entries = view.read(tmp_path)
    lines = view.render_lines(28)

    assert len(entries) == 3
    assert len(lines) == 2
    assert all(len(line) <= 28 for line in lines)
    assert any(line.startswith("claude | succeeded") for line in lines)
    assert any(line.startswith("codex | succeeded") for line in lines)


def test_view_has_explicit_empty_state(tmp_path):
    view = AgentLedgerView()
    assert view.read(tmp_path) == []
    assert view.render_lines(80) == ["No agent runs recorded."]


def test_view_groups_at_most_eight_agents(tmp_path):
    for index in range(10):
        record(tmp_path, agent=f"agent-{index}", status="succeeded",
               task_brief=f"task-{index}", ts=f"{index:02d}")
    view = AgentLedgerView()
    view.read(tmp_path)
    lines = view.render_lines(120)
    assert len(lines) == 8
    assert len({line.split(" | ", 1)[0] for line in lines}) == 8


def test_agents_slash_opens_bounded_overlay_without_transcript_noise(
        tmp_path, monkeypatch):
    record(tmp_path, agent="opencode", status="succeeded",
           provider="local", session_id="ses_shared", run_id="run_shared",
           task_brief="shared ledger task", outputs=["result.md"], attempt=2,
           elapsed_ms=1250, ts="2026-08-22T01:00:00Z")
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)), backend_factory=FakeBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            conversation = app.query_one("#conversation", Markdown)
            before = conversation.source
            composer = app.query_one("#composer", Input)
            composer.value = "/agents opencode"
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TUIAgentRunsScreen)
            assert app.screen.inspector_kind == "agents"
            assert app.screen.query_one(
                "#agent-runs-options", OptionList).option_count == 1
            heading = str(app.screen.query_one(
                "#agent-runs-heading", Static).content)
            detail = str(app.screen.query_one(
                "#agent-runs-detail", Static).content)
            assert "agent=opencode" in heading
            assert "Provider: local" in detail
            assert "Attempt: 2" in detail and "Elapsed: 1.25s" in detail
            assert "ses_shared" in detail and "run_shared" in detail
            assert "Outputs: result.md" in detail
            assert conversation.source == before
            assert app._backend.prompts == []

    _run(scenario())


def test_agents_overlay_renders_trusted_actor_tree_without_ledger_rows(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)), backend_factory=FakeBackend)
        tree = ActorTreeSnapshot((
            ActorNode("agent_main", "", status="RUNNING", current=True),
            ActorNode("reviewer", "agent_main", name="review", status="RUNNING",
                      current=True, depth=1),
        ))
        screen = TUIAgentRunsScreen(
            build_agent_runs_snapshot(
                LedgerQuerySnapshot((), 0, False, "", 0), actor_tree=tree),
            lambda: build_agent_runs_snapshot(
                LedgerQuerySnapshot((), 0, False, "", 0), actor_tree=tree))
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            rendered = str(app.screen.query_one(
                "#agent-runs-tree", Static).content)
            assert "ACTOR TREE" in rendered
            assert "review [reviewer]" in rendered
            assert app.screen.query_one(
                "#agent-runs-options", OptionList).option_count == 0

    _run(scenario())


def test_agents_overlay_cycles_trusted_actor_history(tmp_path):
    history_tree = ActorTreeSnapshot((
        ActorNode("old-root", "", name="old turn", status="SUCCEEDED"),
        ActorNode("old-child", "old-root", name="provider child", depth=1,
                  status="SUCCEEDED", success=True),
    ), turn_id="turn-old")
    history = ActorHistorySnapshot((ActorHistoryGroup(
        "turn-old", "run-old", "ses-old", "ws-old", "SUCCEEDED",
        "2026-08-23T12:00:02Z", history_tree),))

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)), backend_factory=FakeBackend)
        screen = TUIAgentRunsScreen(
            build_agent_runs_snapshot(
                LedgerQuerySnapshot((), 0, False, "", 0),
                actor_history=history),
            lambda: build_agent_runs_snapshot(
                LedgerQuerySnapshot((), 0, False, "", 0),
                actor_history=history))
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            assert "ACTOR HISTORY" in str(app.screen.query_one(
                "#agent-runs-tree", Static).content)
            await pilot.press("h")
            await pilot.pause()
            rendered = str(app.screen.query_one(
                "#agent-runs-tree", Static).content)
            heading = str(app.screen.query_one(
                "#agent-runs-heading", Static).content)
            assert "provider child [old-child]" in rendered
            assert "actor history turn-old" in heading

    _run(scenario())


def test_agents_overlay_filters_and_refreshes_shared_ledger(tmp_path, monkeypatch):
    record(tmp_path, agent="claude", status="failed", task_brief="review",
           ts="2026-08-22T01:00:00Z")
    record(tmp_path, agent="codex", status="succeeded", task_brief="implement",
           ts="2026-08-22T02:00:00Z")
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)), backend_factory=FakeBackend)
        async with app.run_test(size=(110, 34)) as pilot:
            app._slash_runs(())
            await pilot.pause()
            assert isinstance(app.screen, TUIAgentRunsScreen)
            options = app.screen.query_one("#agent-runs-options", OptionList)
            assert options.option_count == 2

            await pilot.press("a")
            await pilot.pause()
            heading = str(app.screen.query_one(
                "#agent-runs-heading", Static).content)
            assert "agent=claude" in heading and options.option_count == 1

            await pilot.press("s")
            await pilot.pause()
            heading = str(app.screen.query_one(
                "#agent-runs-heading", Static).content)
            assert "status=failed" in heading and options.option_count == 1

            record(tmp_path, agent="claude", status="succeeded",
                   task_brief="new review", ts="2026-08-22T03:00:00Z")
            await pilot.press("r")
            await pilot.pause()
            heading = str(app.screen.query_one(
                "#agent-runs-heading", Static).content)
            assert "Agents (1/3)" in heading
            assert app.query_one("#conversation", Markdown).source.startswith(
                "# pi-batch")

    _run(scenario())


def test_agents_overlay_tracks_current_turn_then_returns_to_terminal_ledger(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)),
                         backend_factory=BlockingBackend)
        async with app.run_test(size=(110, 34)) as pilot:
            assert app._start_turn("active trusted task")
            await _wait_until(app._backend.started.is_set, pilot)
            entry_count = len(app._entries)
            composer = app.query_one("#composer", Input)
            composer.value = "/agents"
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TUIAgentRunsScreen)
            assert app.screen.snapshot.records[0].current is True
            detail = str(app.screen.query_one(
                "#agent-runs-detail", Static).content)
            assert "Source: coordinator (current)" in detail
            assert "Agent: codex" in detail and "Provider: openai" in detail
            assert "active trusted task" in detail and "Exit: pending" in detail
            assert len(app._entries) == entry_count

            app._backend.release.set()
            await _wait_until(lambda: not app._busy, pilot)
            assert app.screen.snapshot.records == ()
            assert app.screen.query_one(
                "#agent-runs-options", OptionList).option_count == 0
            assert len(app._entries) == 2

    _run(scenario())


def test_agent_snapshot_reports_untrusted_rows_and_hard_bounds():
    rows = tuple({"agent": f"agent-{index}", "status": "succeeded",
                  "task_brief": f"task-{index}", "ts": str(index)}
                 for index in range(105))
    ledger = LedgerQuerySnapshot(
        records=rows + ({"status": "failed"},), skipped_records=2,
        truncated=False, error="", bytes_read=1024)
    current = AgentRunRecord(
        "coordinator", "now", "current", "", "running", "active",
        current=True)

    snapshot = build_agent_runs_snapshot(ledger, current)

    assert len(snapshot.records) == 100
    assert snapshot.records[0] == current
    assert snapshot.skipped_records == 3
    assert snapshot.truncated is True
    assert snapshot.ledger_bytes == 1024


def test_help_exposes_agents_inspector_and_runs_alias():
    assert "`/agents [agent]`" in SLASH_HELP
    assert "`/runs [agent]`" in SLASH_HELP


def test_breaker_view_and_slash_command_use_project_state(tmp_path, monkeypatch):
    state_file = tmp_path / "provider-breaker.json"
    state_file.write_text(json.dumps({
        "streak": 0, "step": 2,
        "cooldown_until": time.monotonic() + 30,
    }), encoding="utf-8")
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)

    lines = render_breaker_lines(tmp_path, width=100)
    assert lines[0] == "PROVIDER BREAKER"
    assert "ACTIVE" in lines[1] and "step 2" in lines[1]

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)), backend_factory=FakeBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "/breaker"
            await pilot.press("enter")
            await pilot.pause()
            source = app.query_one("#conversation", Markdown).source
            assert "PROVIDER BREAKER" in source and "ACTIVE" in source
            assert app._backend.prompts == []

    _run(scenario())


def test_breaker_view_lists_active_provider_partitions(tmp_path, monkeypatch):
    from pbatch import provider_breaker
    monkeypatch.setattr(provider_breaker, "breaker_state", lambda _data_dir=None: {
        "active": True, "cooldown_remaining_s": 30.0, "streak": 0,
        "step": 1, "threshold": 5, "shared_state_file": str(
            tmp_path / "provider-breaker.json"),
        "active_providers": ["provider:deepseek"], "provider_count": 2,
    })
    lines = render_breaker_lines(tmp_path, width=120)
    assert any("provider:deepseek" in line for line in lines)
    assert not any("agent:codex" in line for line in lines)


def test_runs_view_renders_shared_ledger(tmp_path):
    """/runs 面板数据 = 共享账本（与 ops runs 同源），含 attempt 痕迹。"""
    import json as json_mod
    from pbatch import config as config_mod
    from pbatch.tui_agents import RunsView
    data_dir = config_mod.data_dir(str(tmp_path))
    (data_dir / "agents").mkdir(parents=True, exist_ok=True)
    ledger = data_dir / "agents" / "ledger.jsonl"
    with open(ledger, "a", encoding="utf-8") as f:
        f.write(json_mod.dumps({
            "ts": "2026-08-22T09:00:00Z", "agent": "pi", "status": "succeeded",
            "attempt": 1, "task_brief": "normal task", "outputs": [],
            "elapsed_ms": 100, "exit_code": 0}) + "\n")
        f.write(json_mod.dumps({
            "ts": "2026-08-22T09:01:00Z", "agent": "codex", "status": "rejected",
            "attempt": 2, "task_brief": "failed once then fixed",
            "outputs": [], "elapsed_ms": 900, "exit_code": 0}) + "\n")
    lines = RunsView(limit=10).render_lines(str(data_dir), width=90)
    assert any("succeeded" in line and "#1" in line for line in lines)
    assert any("rejected" in line and "#2" in line for line in lines)
    assert "RUNS" in lines[0]


def test_ops_report_html_embeds_ledger_summary(tmp_path):
    import json as json_mod
    import subprocess, sys
    from pbatch import config as config_mod
    from pbatch.ops import report_html
    from pbatch.tui_agents import RunsView
    data_dir = config_mod.data_dir(str(tmp_path))
    (data_dir / "agents").mkdir(parents=True, exist_ok=True)
    (data_dir / "provider-breaker.json").write_text(json_mod.dumps({
        "streak": 0, "step": 1, "cooldown_until": 0}), encoding="utf-8")
    (data_dir / "agents" / "ledger.jsonl").write_text(
        '{"ts":"2026-08-22T09:00:00Z","agent":"pi","status":"succeeded",'
        '"attempt":1,"task_brief":"t","outputs":[],"elapsed_ms":1,'
        '"exit_code":0}\n', encoding="utf-8")
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "run.log").write_text(
        "line-one\nPROVIDER BREAKER: cooldown\nline-three\n", encoding="utf-8")
    page = report_html(str(data_dir), str(tmp_path))
    assert "pi-batch" in page and "succeeded" in page and "运营控制台" in page
    assert "运营日志" in page and "PROVIDER BREAKER" in page
    assert "Provider breaker: ok (streak 0/" in page


def test_ops_report_names_active_provider_partition(tmp_path, monkeypatch):
    from pbatch import config as config_mod
    from pbatch import provider_breaker
    from pbatch.ops import report_html
    data_dir = config_mod.data_dir(str(tmp_path))
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(provider_breaker, "breaker_state", lambda _data_dir=None: {
        "active": True, "cooldown_remaining_s": 30.0, "streak": 0,
        "step": 1, "threshold": 5, "shared_state_file": str(
            data_dir / "provider-breaker.json"),
        "active_providers": ["provider:deepseek"], "provider_count": 1,
    })
    page = report_html(str(data_dir), str(tmp_path))
    assert "Provider breaker: ACTIVE" in page
    assert "provider:deepseek" in page
