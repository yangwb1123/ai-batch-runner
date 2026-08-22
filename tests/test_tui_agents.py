"""Pure projection and headless TUI contracts for ``/agents``."""

from __future__ import annotations

import asyncio

from textual.widgets import Input, Markdown

from pbatch import config
from pbatch.agent_ledger import record
from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_agents import AgentLedgerView


class FakeBackend:
    def __init__(self, emit, **_options):
        self.emit = emit
        self.prompts = []

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        return {"success": True, "stdout": "", "stderr": ""}

    def snapshot(self):
        return {"status": "idle", "session_name": "agents-test"}


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


def test_agents_slash_command_renders_ledger_without_agent_turn(tmp_path, monkeypatch):
    record(tmp_path, agent="opencode", status="succeeded",
           task_brief="shared ledger task", ts="2026-08-22T01:00:00Z")
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)), backend_factory=FakeBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "/agents"
            await pilot.press("enter")
            await pilot.pause()
            source = app.query_one("#conversation", Markdown).source
            assert "opencode | succeeded" in source
            assert "shared ledger task" in source
            assert app._backend.prompts == []

    _run(scenario())
