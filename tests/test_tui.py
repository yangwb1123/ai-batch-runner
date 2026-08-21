"""Headless product-TUI interaction contracts."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from textual.widgets import Footer, Header, Input, Markdown, Static

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_backend import TUIBackend


class FakeBackend:
    def __init__(self, emit, **options):
        self.emit = emit
        self.prompts = []
        self.new_sessions = []
        self.cancel_calls = 0
        self.agent = options.get("agent") or "fake"
        self.model = options.get("model", "")
        self.provider = options.get("provider", "")
        self.memory_mode = options.get("memory_mode", "auto")

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        self.emit("stdout", "streamed answer")
        self.emit("stderr", "streamed warning")
        return {"success": True, "stdout": "streamed answer",
                "stderr": "streamed warning"}

    def snapshot(self):
        return {"status": "idle", "session_name": "test",
                "turn": len(self.prompts)}

    def new_session(self, name=""):
        self.new_sessions.append(name)
        return {"session_name": name or "test-new"}

    def cancel(self):
        self.cancel_calls += 1
        return True


class BlockingBackend(FakeBackend):
    def __init__(self, emit, **options):
        super().__init__(emit, **options)
        self.started = threading.Event()
        self.release = threading.Event()

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        self.started.set()
        self.release.wait(timeout=5)
        self.emit("stdout", "released")
        return {"success": True, "stdout": "released"}

    def cancel(self):
        self.cancel_calls += 1
        self.release.set()
        return True


async def _wait_until(predicate, pilot, attempts=100):
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition did not become true")


def test_first_screen_has_product_shell():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=FakeBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one(Header)
            assert app.query_one(Footer)
            assert app.query_one("#composer", Input)
            assert app.query_one("#status-panel", Static)
            assert "Describe the work" in app.query_one("#conversation", Markdown).source

    asyncio.run(scenario())


def test_narrow_terminal_hides_sidebar_but_keeps_composer():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=FakeBackend)
        async with app.run_test(size=(50, 16)) as pilot:
            await pilot.pause()
            assert app.query_one("#status-panel", Static).display is False
            assert app.query_one("#composer", Input).display is True

    asyncio.run(scenario())


def test_turn_streams_stdout_and_stderr_once():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=FakeBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "hello"
            await pilot.press("enter")
            await _wait_until(lambda: not app._busy, pilot)
            source = app.query_one("#conversation", Markdown).source
            assert app._backend.prompts == ["hello"]
            assert source.count("streamed answer") == 1
            assert source.count("streamed warning") == 1

    asyncio.run(scenario())


def test_app_and_real_backend_stream_end_to_end(tmp_path: Path):
    agent = tmp_path / "agent.sh"
    agent.write_text("#!/bin/sh\nprintf 'real backend answer\\n'\n",
                     encoding="utf-8")
    agent.chmod(0o755)
    backend = TUIBackend(cwd=str(tmp_path), agent=str(agent),
                         memory_mode="off", session_id="ui-real-session")

    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "real prompt"
            await pilot.press("enter")
            await _wait_until(lambda: not app._busy, pilot)
            source = app.query_one("#conversation", Markdown).source
            assert source.count("real backend answer") == 1
            assert backend.snapshot()["turn"] == 1
            assert not (tmp_path / ".pi-batch.lock").exists()

    asyncio.run(scenario())


def test_unknown_slash_command_never_calls_backend():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=FakeBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "/definitely-unknown"
            await pilot.press("enter")
            await pilot.pause()
            assert app._backend.prompts == []
            assert "Unknown slash command" in app.query_one(
                "#conversation", Markdown).source

    asyncio.run(scenario())


def test_busy_state_prevents_duplicate_submission():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=BlockingBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            app._start_turn("first")
            await _wait_until(app._backend.started.is_set, pilot)
            assert app.query_one("#composer", Input).disabled
            app._start_turn("second")
            assert app._backend.prompts == ["first"]
            app._backend.release.set()
            await _wait_until(lambda: not app._busy, pilot)

    asyncio.run(scenario())


def test_new_clear_and_cancel_lifecycle():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=BlockingBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            app._start_turn("long task")
            await _wait_until(app._backend.started.is_set, pilot)
            app.action_new_session()
            assert app._backend.new_sessions == []
            app.action_cancel()
            await _wait_until(lambda: not app._busy, pilot)
            assert app._backend.cancel_calls == 1

            app.action_new_session()
            assert app._backend.new_sessions == [""]
            assert "New session started" in app.query_one(
                "#conversation", Markdown).source
            app._append_entry("system", "remove-me")
            app.action_clear()
            assert "remove-me" not in app.query_one("#conversation", Markdown).source

    asyncio.run(scenario())
