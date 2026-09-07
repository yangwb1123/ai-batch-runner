"""TUI product-direction command contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Input, Markdown

from pbatch.tui import PiBatchTUI, TuiOptions


class _ProductBackend:
    def __init__(self, emit, **options):
        self.emit = emit
        self.cwd = str(Path(options.get("cwd") or ".").resolve())
        self.prompts = []

    def set_emit(self, emit):
        self.emit = emit

    def snapshot(self):
        return {"status": "idle", "session_name": "product-test"}

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        return {"success": True, "stdout": "agent", "stderr": ""}

    def new_session(self, name=""):
        return {"status": "idle", "session_name": name or "product-test"}

    def cancel(self):
        return True


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def test_tui_product_command_is_local_and_renders_brief(tmp_path):
    async def scenario():
        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path)), backend_factory=_ProductBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "/product 做一个多租户审批平台，支持登录和审批"
            await pilot.press("enter")
            await pilot.pause()
            source = app.query_one("#conversation", Markdown).source
            assert "# 产品方向简报" in source
            assert "状态机（候选）" in source
            assert "待确认" in source
            assert app._backend.prompts == []
            assert app._last_status == "Product direction ready"

    _run(scenario())


def test_tui_product_command_uses_latest_task_when_requirement_omitted(tmp_path):
    async def scenario():
        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path)), backend_factory=_ProductBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            app._last_task_prompt = "做一个开源文件上传产品"
            app._slash_product(())
            await pilot.pause()
            source = app.query_one("#conversation", Markdown).source
            assert "做一个开源文件上传产品" in source
            assert app._backend.prompts == []

    _run(scenario())


def test_tui_product_does_not_cross_new_session_boundary(tmp_path):
    async def scenario():
        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path)), backend_factory=_ProductBackend)
        async with app.run_test(size=(100, 30)) as pilot:
            app._last_task_prompt = "上一会话的商业化产品"
            app._new_session()
            app._slash_product(())
            await pilot.pause()
            source = app.query_one("#conversation", Markdown).source
            assert "上一会话的商业化产品" not in source
            assert "Usage: `/product <requirement>`" in source
            assert app._backend.prompts == []

    _run(scenario())
