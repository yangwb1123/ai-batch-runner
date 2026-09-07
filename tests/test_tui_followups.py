"""Active-turn follow-up queue and capability-driven steer contracts."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from textual.widgets import Input, Markdown, OptionList

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_controller import TUIEventController
from pbatch.tui_followups import (
    FollowUpQueue,
    TUIFollowUpScreen,
)
from pbatch.tui_protocol import RuntimeCapabilities, RuntimeEvent, TurnState
from pbatch.tui_runtime import RuntimeInfo
from pbatch.tui_shell import SLASH_HELP


class ControlledBackend:
    def __init__(self, emit, **_options):
        self.emit = emit
        self.prompts: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.first_success = True

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            self.started.set()
            self.release.wait(timeout=5)
            success = self.first_success
        else:
            success = True
        text = f"answer:{prompt}"
        self.emit("stdout", text)
        return {"success": success, "stdout": text,
                "reason": "first failed" if not success else ""}

    def snapshot(self):
        return {"status": "running" if self.started.is_set() else "idle",
                "session_id": "ses_followups"}

    def new_session(self, _name=""):
        return {"session_name": "new", "session_id": "ses_new"}

    def cancel(self):
        self.release.set()
        return True


class SteerAdapter:
    def __init__(self):
        self.requests = []
        self.info = RuntimeInfo(
            "steer-test", 1,
            RuntimeCapabilities(
                runtime="steer-test", capabilities={"turn_steer": True}))

    def initialize(self, _request):
        raise AssertionError("active capability checks must not reinitialize runtime")

    def runtime_info(self):
        return self.info

    def steer_turn(self, request):
        self.requests.append(request)


def _event(seq: int, kind: str, payload=None) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=f"evt_followup_{seq}", seq=seq,
        occurred_at=f"2026-08-25T12:00:0{seq}Z",
        workspace_id="ws_followup", session_id="ses_followup",
        run_id="run_followup", turn_id="turn_followup",
        actor_id="agent_main", kind=kind, payload=payload or {})


def test_follow_up_queue_is_fifo_editable_and_bounded():
    queue = FollowUpQueue(max_items=2, max_bytes=20, item_max_bytes=12)

    first = queue.enqueue(" first ", "ses_one")
    second = queue.enqueue("second", "ses_one")
    assert [item.text for item in queue.items] == ["first", "second"]
    assert queue.bytes_used == 11
    assert queue.edit(2, "changed").text == "changed"
    assert queue.remove(1) == first
    assert queue.peek().item_id == second.item_id

    queue.enqueue("third", "ses_one")
    with pytest.raises(OverflowError, match="item limit"):
        queue.enqueue("fourth", "ses_one")
    with pytest.raises(ValueError, match="exceeds 12 bytes"):
        queue.edit(1, "x" * 13)


def test_help_exposes_queue_and_capability_driven_steer():
    assert "`/queue [TEXT|list|run|clear|drop N|edit N TEXT]`" in SLASH_HELP
    assert "`/steer <text>`" in SLASH_HELP


def test_busy_plain_input_queues_then_runs_after_success():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=ControlledBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            app._start_turn("first")
            await _wait_until(app._backend.started.is_set, pilot)
            composer = app.query_one("#composer", Input)
            assert not composer.disabled
            assert "Queue follow-up" in composer.placeholder

            composer.value = "follow up"
            await pilot.press("enter")
            assert [item.text for item in app._follow_up_queue.items] == [
                "follow up"]
            assert app._backend.prompts == ["first"]

            app._backend.release.set()
            await _wait_until(lambda: len(app._backend.prompts) == 2, pilot)
            await _wait_until(lambda: not app._busy, pilot)
            assert app._backend.prompts == ["first", "follow up"]
            assert app._follow_up_queue.items == ()
            source = app.query_one("#conversation", Markdown).source
            assert "follow up" in source and "answer:follow up" in source

    _run(scenario())


def test_failed_turn_preserves_queue_until_explicit_run():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=ControlledBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            app._backend.first_success = False
            app._start_turn("first")
            await _wait_until(app._backend.started.is_set, pilot)
            app._enqueue_follow_up("preserved")
            app._backend.release.set()
            await _wait_until(lambda: not app._busy, pilot)

            assert app._backend.prompts == ["first"]
            assert app._follow_up_queue.peek().text == "preserved"
            assert "paused" in app._last_status.lower()

            assert app._run_next_follow_up()
            await _wait_until(lambda: not app._busy, pilot)
            assert app._backend.prompts == ["first", "preserved"]
            assert app._follow_up_queue.items == ()

    _run(scenario())


def test_unknown_trusted_terminal_pauses_queue_despite_success_result():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=ControlledBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            app._set_busy(True)
            app._enqueue_follow_up("preserve after unknown")
            app._projection_driven = True
            app._runtime_view = replace(
                app._runtime_view, turn_state=TurnState.UNKNOWN)

            app._schedule_follow_up_after_turn({"success": True}, None)
            await pilot.pause()

            assert app._follow_up_queue.peek().text == "preserve after unknown"
            assert "paused" in app._last_status.lower()
            app._projection_driven = False
            app._set_busy(False)

    _run(scenario())


def test_queue_overlay_drops_items_without_transcript_noise():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=ControlledBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            app._start_turn("first")
            await _wait_until(app._backend.started.is_set, pilot)
            app._enqueue_follow_up("inspect me")
            before = app.query_one("#conversation", Markdown).source

            composer = app.query_one("#composer", Input)
            composer.value = "/queue"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TUIFollowUpScreen)
            assert app.screen.query_one("#follow-up-options", OptionList).option_count == 1
            assert app.query_one("#conversation", Markdown).source == before

            await pilot.press("d")
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, TUIFollowUpScreen)
            assert app._follow_up_queue.items == ()
            assert app.query_one("#conversation", Markdown).source == before
            app.screen.action_close()
            app._backend.release.set()
            await _wait_until(lambda: not app._busy, pilot)

    _run(scenario())


def test_explicit_steer_requires_capability_and_active_handle():
    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend_factory=ControlledBackend)
        async with app.run_test(size=(120, 40)) as pilot:
            adapter = SteerAdapter()
            app._adapter = adapter
            app._adapter_session_id = "ses_steer"
            app._turn_handle = SimpleNamespace(turn_id="turn_steer", done=False)
            app._set_busy(True)

            composer = app.query_one("#composer", Input)
            composer.value = "/steer focus on tests"
            await pilot.press("enter")
            await _wait_until(lambda: bool(adapter.requests), pilot)
            await _wait_until(lambda: not app._steer_pending, pilot)
            assert adapter.requests == [{
                "turn_id": "turn_steer", "text": "focus on tests"}]
            assert app._follow_up_queue.items == ()
            assert app._last_status == "Steer accepted"
            app._turn_handle = None
            app._adapter = None
            app._set_busy(False)

    _run(scenario())


def test_trusted_turn_steered_event_enters_timeline():
    controller = TUIEventController()
    controller.ingest(_event(1, "turn.started"))

    view = controller.ingest(_event(
        2, "turn.steered", {"text": "focus on tests"}))

    assert view is not None
    assert view.timeline.entries[-1].kind == "status"
    assert view.timeline.entries[-1].summary.startswith("Turn steered")
    assert "focus on tests" in view.timeline.entries[-1].detail


async def _wait_until(predicate, pilot, attempts=200):
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition did not become true")


def _run(coro) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(
                *pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
