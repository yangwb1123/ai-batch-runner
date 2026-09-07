"""Failure-first TUI contracts for typed Pi session controls."""

from __future__ import annotations

import asyncio
import queue
import threading
import time

from textual.widgets import Input, Markdown

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_protocol import RuntimeCapabilities
from pbatch.tui_runtime import (
    CompactionHandle,
    RuntimeCompactionResult,
    RuntimeContextUsage,
    RuntimeInfo,
    RuntimeModelRef,
    RuntimeSession,
    RuntimeSessionState,
    RuntimeSessionStats,
    RuntimeTokenTotals,
)
from pbatch.tui_session_controls import (
    render_compaction_result,
    render_session_control,
)
from pbatch.tui_shell import SLASH_HELP


class ControlAdapter:
    """Small typed adapter whose long-running results are test-controlled."""

    def __init__(self, *, capabilities=()) -> None:
        enabled = {name: True for name in capabilities}
        self.info = RuntimeInfo(
            "pi-rpc", 1,
            RuntimeCapabilities(runtime="pi-rpc", capabilities=enabled),
        )
        self.session_id = "ses_control"
        self.provider_session_id = "/sessions/control.jsonl"
        self.state_calls = []
        self.stats_calls = []
        self.compact_calls = []
        self.interrupt_calls = []
        self.handles: list[CompactionHandle] = []
        self.events_queue: queue.Queue = queue.Queue()
        self.state_started = threading.Event()
        self.state_gate: threading.Event | None = None
        self.closed = False
        self.state = RuntimeSessionState(
            self.session_id, self.provider_session_id,
            name="control", model=RuntimeModelRef(
                "anthropic", "claude-test", "Claude Test"),
            thinking_level="medium", is_streaming=False,
            is_compacting=False, auto_compaction_enabled=True,
            message_count=7, pending_message_count=2,
        )
        self.stats = RuntimeSessionStats(
            self.session_id, self.provider_session_id,
            user_messages=3, assistant_messages=4,
            tool_calls=5, tool_results=5, total_messages=17,
            tokens=RuntimeTokenTotals(100, 20, 30, 4, 154),
            cost=0.125,
            context_usage=RuntimeContextUsage(80, 200, 40.0),
        )

    def runtime_info(self):
        return self.info

    def initialize(self, _request):
        return self.info

    def start_session(self, request):
        return RuntimeSession(
            self.session_id, self.provider_session_id,
            str(request.get("workspace_id", "")),
            "2026-08-26T00:00:00Z", "ACTIVE",
        )

    def events(self):
        return self.events_queue

    def get_session_state(self, request):
        self.state_calls.append(request)
        self.state_started.set()
        if self.state_gate is not None:
            self.state_gate.wait(timeout=2)
        return self.state

    def get_session_stats(self, request):
        self.stats_calls.append(request)
        return self.stats

    def start_compaction(self, request):
        self.compact_calls.append(request)
        handle = CompactionHandle(
            f"compact_{len(self.handles) + 1}", self.session_id,
            "2026-08-26T00:00:00Z",
        )
        self.handles.append(handle)
        return handle

    def interrupt_turn(self, request):
        self.interrupt_calls.append(request)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for handle in self.handles:
            if not handle.done:
                handle._set_exception(
                    RuntimeError("control transport closed"), state="UNKNOWN")
        self.events_queue.put(None)


class ControlBackend:
    agent = "pi"
    provider = ""
    model = ""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.turn_calls = []
        self.cancel_calls = 0

    def set_emit(self, _emit) -> None:
        return None

    def run_turn(self, prompt):
        self.turn_calls.append(prompt)
        return {"success": True, "stdout": "", "stderr": ""}

    def cancel(self):
        self.cancel_calls += 1
        return True

    def snapshot(self):
        return {"status": "idle", "session_id": "ses_control"}


def _app(tmp_path, adapter: ControlAdapter) -> PiBatchTUI:
    return PiBatchTUI(
        TuiOptions(cwd=str(tmp_path), agent="pi", runtime="pi-rpc"),
        backend=ControlBackend(str(tmp_path)),
        pi_rpc_factory=lambda _cwd, **_kwargs: adapter,
    )


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


async def _submit(app: PiBatchTUI, pilot, text: str) -> None:
    composer = app.query_one("#composer", Input)
    composer.value = text
    await pilot.press("enter")
    await pilot.pause()


async def _wait_until(predicate, pilot, attempts: int = 300) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition did not become true")


def _result(summary: str = "late authoritative summary"):
    return RuntimeCompactionResult(
        "ses_control", summary, tokens_before=1000,
        estimated_tokens_after=250,
        tokens=RuntimeTokenTotals(100, 20, 30, 4, 154),
        cost=0.125,
    )


def test_session_control_renderers_strip_control_text_and_bound_every_line():
    hostile = "\x1b[31m" + ("very-long-model-name-" * 2_000)
    state = RuntimeSessionState(
        "ses-control", "/provider/" + hostile,
        name=hostile, model=RuntimeModelRef("provider", "model", hostile),
        thinking_level=hostile, is_streaming=False, is_compacting=False,
        auto_compaction_enabled=True, message_count=7,
        pending_message_count=2,
    )
    stats = RuntimeSessionStats(
        "ses-control", "/provider/control", total_messages=17,
        tokens=RuntimeTokenTotals(total_tokens=154), cost=0.125,
        context_usage=RuntimeContextUsage(80, 200, 40.0),
    )

    session_text = render_session_control(state, stats, width=48)
    compact_text = render_compaction_result(
        _result("\x1b]8;;https://invalid\x07" + "summary-" * 20_000),
        width=48,
    )

    for rendered in (session_text, compact_text):
        assert "\x1b" not in rendered and "\x07" not in rendered
        assert len(rendered.encode("utf-8")) <= 32 * 1024
        assert len(rendered.splitlines()) <= 64
        assert all(len(line) <= 48 for line in rendered.splitlines())
    assert "154" in session_text and "40" in session_text
    assert ("1000" in compact_text or "1,000" in compact_text)
    assert "250" in compact_text


def test_unsupported_commands_fail_before_any_adapter_control_call(tmp_path):
    async def scenario():
        adapter = ControlAdapter()
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(app, pilot, "/session")
            await _submit(app, pilot, "/compact keep decisions")
            assert adapter.state_calls == []
            assert adapter.stats_calls == []
            assert adapter.compact_calls == []

    _run(scenario())


def test_session_is_read_only_during_turn_but_compact_is_rejected_busy(tmp_path):
    async def scenario():
        adapter = ControlAdapter(capabilities=(
            "session_state", "session_stats", "session_compact"))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            app._busy = True
            await _submit(app, pilot, "/session")
            await _wait_until(
                lambda: len(adapter.state_calls) == len(adapter.stats_calls) == 1,
                pilot,
            )
            await _submit(app, pilot, "/compact must not start")
            assert adapter.compact_calls == []
            app._busy = False

    _run(scenario())


def test_late_session_query_is_discarded_after_runtime_boundary_changes(
        tmp_path):
    async def scenario():
        adapter = ControlAdapter(capabilities=(
            "session_state", "session_stats"))
        adapter.state_gate = threading.Event()
        replacement = ControlAdapter(capabilities=(
            "session_state", "session_stats"))
        replacement.session_id = "ses_replacement"
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            before = app.query_one("#conversation", Markdown).source
            await _submit(app, pilot, "/session")
            await _wait_until(adapter.state_started.is_set, pilot)
            assert app.query_one("#conversation", Markdown).source == before

            app._adapter = replacement
            app._adapter_session_id = "ses_replacement"
            adapter.state_gate.set()
            await pilot.pause(0.1)

            assert app.query_one("#conversation", Markdown).source == before
            adapter.close()

    _run(scenario())


def test_compact_returns_immediately_rejects_duplicate_and_renders_late_once(
        tmp_path):
    async def scenario():
        adapter = ControlAdapter(capabilities=("session_compact",))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            started = time.monotonic()
            app._slash_compact(("keep", "decisions"))
            assert time.monotonic() - started < 0.2
            await _wait_until(lambda: len(adapter.compact_calls) == 1, pilot)
            request = adapter.compact_calls[0]
            assert request.session_id == "ses_control"
            assert request.instructions == "keep decisions"
            assert app._busy is False

            app._slash_compact(("duplicate",))
            await pilot.pause()
            assert len(adapter.compact_calls) == 1

            adapter.handles[0]._set_result(_result())
            await _wait_until(
                lambda: "late authoritative summary" in app.query_one(
                    "#conversation", Markdown).source,
                pilot,
            )
            source = app.query_one("#conversation", Markdown).source
            assert source.count("late authoritative summary") == 1
            adapter.handles[0]._set_result(_result("forged replacement"))
            await pilot.pause(0.05)
            assert "forged replacement" not in app.query_one(
                "#conversation", Markdown).source

    _run(scenario())


def test_compaction_lock_rejects_turn_and_cancel_never_targets_other_work(
        tmp_path):
    async def scenario():
        adapter = ControlAdapter(capabilities=("session_compact",))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            app._slash_compact(("focus",))
            await _wait_until(lambda: len(adapter.handles) == 1, pilot)
            assert app._busy is False

            assert app._start_turn("must remain blocked") is False
            app.action_cancel()
            await pilot.pause()

            assert app._backend.turn_calls == []
            assert app._backend.cancel_calls == 0
            assert adapter.interrupt_calls == []
            assert adapter.handles[0].done is False

            adapter.handles[0]._set_result(_result())
            await _wait_until(
                lambda: "late authoritative summary" in app.query_one(
                    "#conversation", Markdown).source,
                pilot,
            )

    _run(scenario())


def test_late_compaction_is_discarded_after_adapter_session_boundary_changes(
        tmp_path):
    async def scenario():
        adapter = ControlAdapter(capabilities=("session_compact",))
        replacement = ControlAdapter(capabilities=("session_compact",))
        replacement.session_id = "ses_replacement"
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            app._slash_compact(("focus",))
            await _wait_until(lambda: len(adapter.compact_calls) == 1, pilot)
            before = app.query_one("#conversation", Markdown).source
            app._adapter = replacement
            app._adapter_session_id = "ses_replacement"
            adapter.handles[0]._set_result(_result("stale session summary"))
            await pilot.pause(0.1)
            assert app.query_one("#conversation", Markdown).source == before
            adapter.close()

    _run(scenario())


def test_compaction_error_and_transport_close_render_once_without_escape_text(
        tmp_path):
    async def scenario():
        adapter = ControlAdapter(capabilities=("session_compact",))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            app._slash_compact(("focus",))
            await _wait_until(lambda: len(adapter.handles) == 1, pilot)
            adapter.handles[0]._set_exception(
                RuntimeError("\x1b[31mprovider compaction failed\x1b[0m"))
            await _wait_until(
                lambda: "provider compaction failed" in app.query_one(
                    "#conversation", Markdown).source,
                pilot,
            )
            source = app.query_one("#conversation", Markdown).source
            assert source.count("provider compaction failed") == 1
            assert "\x1b" not in source

            app._slash_compact(("again",))
            await _wait_until(lambda: len(adapter.handles) == 2, pilot)
            adapter.close()
            await _wait_until(
                lambda: "control transport closed" in app.query_one(
                    "#conversation", Markdown).source,
                pilot,
            )
            source = app.query_one("#conversation", Markdown).source
            assert source.count("control transport closed") == 1

    _run(scenario())


def test_help_advertises_typed_session_controls():
    assert "`/session`" in SLASH_HELP
    assert "`/compact" in SLASH_HELP
