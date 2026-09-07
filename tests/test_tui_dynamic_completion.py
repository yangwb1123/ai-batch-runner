"""Failure-first safety contracts for dynamic Pi runtime completion."""

from __future__ import annotations

import asyncio
import queue
import threading

import pytest
from textual.widgets import Input

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_model_picker import (
    RuntimeSettingPickerAction,
    TUIRuntimeSettingPickerScreen,
)
from pbatch.tui_protocol import RuntimeCapabilities
from pbatch.tui_runtime import (
    RuntimeInfo,
    RuntimeModelCatalog,
    RuntimeModelRef,
    RuntimeSession,
    RuntimeSessionState,
    RuntimeThinkingLevels,
)
from pbatch.tui_runtime_completion import _runtime_completion_value
from pbatch.tui_runtime_completion import (
    _bounded_completion_choices,
    parse_runtime_completion_context,
)


class _Backend:
    agent = "pi"
    provider = ""
    model = ""
    memory_mode = "off"

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.turn_calls: list[str] = []

    def set_emit(self, _emit) -> None:
        return None

    def run_turn(self, prompt: str):
        self.turn_calls.append(prompt)
        return {"success": True, "stdout": "", "stderr": ""}

    def snapshot(self):
        return {"status": "idle", "session_id": "ses_completion"}


class _CompletionAdapter:
    def __init__(
            self, *, scope: str = "session", allow_writes: bool = False) -> None:
        capabilities = RuntimeCapabilities(runtime="pi-rpc", capabilities={
            "structured_events": True,
            "message_stream": True,
            "interrupt": True,
            "session_state": True,
            "model_catalog": True,
            "thinking_levels": True,
            "model_select": True,
            "thinking_select": True,
        })
        self.info = RuntimeInfo(
            "pi-rpc", 1, capabilities,
            runtime_version="0.84.1" if scope == "global" else "0.84.3",
            settings_scope=scope,
        )
        self.session_id = "ses_completion"
        self.events_queue: queue.Queue = queue.Queue()
        self.models = RuntimeModelCatalog(self.session_id, (
            RuntimeModelRef("anthropic", "claude-current", "Current"),
            RuntimeModelRef("openrouter", "anthropic/claude-next", "Next"),
        ))
        self.levels = RuntimeThinkingLevels(
            self.session_id, ("off", "high", "max"))
        self.model_reads: list[object] = []
        self.level_reads: list[object] = []
        self.state_reads: list[object] = []
        self.model_writes: list[object] = []
        self.thinking_writes: list[object] = []
        self.model_started = threading.Event()
        self.model_finished = threading.Event()
        self.model_gate: threading.Event | None = None
        self.model_error: Exception | None = None
        self.allow_writes = allow_writes

    def runtime_info(self):
        return self.info

    def initialize(self, _request):
        return self.info

    def start_session(self, request):
        return RuntimeSession(
            self.session_id, "/sessions/completion.jsonl",
            str(request.get("workspace_id", "")),
            "2026-08-27T00:00:00Z", "ACTIVE")

    def events(self):
        return self.events_queue

    def get_session_state(self, request):
        self.state_reads.append(request)
        return RuntimeSessionState(
            self.session_id, "/sessions/completion.jsonl",
            model=self.models.models[0], thinking_level="off")

    def get_available_models(self, request):
        self.model_reads.append(request)
        self.model_started.set()
        if self.model_gate is not None:
            self.model_gate.wait(timeout=2)
        self.model_finished.set()
        if self.model_error is not None:
            raise self.model_error
        return self.models

    def get_available_thinking_levels(self, request):
        self.level_reads.append(request)
        return self.levels

    def set_model(self, request):
        self.model_writes.append(request)
        if self.allow_writes:
            return RuntimeSessionState(
                self.session_id, "/sessions/completion.jsonl",
                model=RuntimeModelRef(request.provider, request.model_id),
                thinking_level="high")
        raise AssertionError("completion must not call set_model")

    def set_thinking_level(self, request):
        self.thinking_writes.append(request)
        if self.allow_writes:
            return RuntimeSessionState(
                self.session_id, "/sessions/completion.jsonl",
                model=self.models.models[0], thinking_level=request.level)
        raise AssertionError("completion must not call set_thinking_level")

    def close(self):
        if self.model_gate is not None:
            self.model_gate.set()
        self.events_queue.put(None)


def _app(tmp_path, adapter: _CompletionAdapter) -> PiBatchTUI:
    return PiBatchTUI(
        TuiOptions(cwd=str(tmp_path), agent="pi", runtime="pi-rpc"),
        backend=_Backend(str(tmp_path)),
        pi_rpc_factory=lambda _cwd, **_kwargs: adapter)


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


async def _wait_until(predicate, pilot, attempts: int = 300) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("condition did not become true")


async def _open_completion(app, pilot, value: str, *, key: str = "tab"):
    composer = app.query_one("#composer", Input)
    composer.value = value
    composer.cursor_position = len(value)
    await pilot.pause()
    await pilot.press(key)
    await _wait_until(
        lambda: isinstance(app.screen, TUIRuntimeSettingPickerScreen), pilot)
    screen = app.screen
    assert screen.completion_only is True
    return composer, screen


@pytest.mark.parametrize(("kind", "first", "second", "expected"), (
    ("model", "/model next", "/model current",
     "/model anthropic/claude-current"),
    ("thinking", "/thinking hi", "/thinking max", "/thinking max"),
))
def test_one_snapshot_is_reused_locally_and_completion_never_sets(
        tmp_path, kind, first, second, expected):
    async def scenario():
        adapter = _CompletionAdapter()
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            _composer, first_screen = await _open_completion(
                app, pilot, first)
            snapshot = first_screen.choices
            await pilot.press("escape")
            await _wait_until(
                lambda: not isinstance(
                    app.screen, TUIRuntimeSettingPickerScreen), pilot)

            adapter.models = RuntimeModelCatalog(adapter.session_id, ())
            adapter.levels = RuntimeThinkingLevels(adapter.session_id, ("off",))
            composer, second_screen = await _open_completion(
                app, pilot, second)
            assert second_screen.choices is snapshot
            await pilot.press("enter")
            await _wait_until(lambda: composer.value == expected, pilot)

            assert len(adapter.model_reads) == int(kind == "model")
            assert len(adapter.level_reads) == int(kind == "thinking")
            assert adapter.state_reads == []
            assert adapter.model_writes == adapter.thinking_writes == []
            assert app._backend.turn_calls == []

    _run(scenario())


@pytest.mark.parametrize(("value", "expected"), (
    ("/model next", "/model openrouter/anthropic/claude-next"),
    ("/thinking hi", "/thinking high"),
))
def test_old_global_scope_completion_remains_read_only(
        tmp_path, value, expected):
    async def scenario():
        adapter = _CompletionAdapter(scope="global")
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = value
            composer.cursor_position = len(value)
            await pilot.pause()
            await pilot.press("tab")
            await _wait_until(
                lambda: isinstance(
                    app.screen, TUIRuntimeSettingPickerScreen), pilot)
            assert app.screen.completion_only is True
            await pilot.press("enter")
            await _wait_until(lambda: composer.value == expected, pilot)

            assert adapter.state_reads == []
            assert adapter.model_writes == adapter.thinking_writes == []
            assert app.options.model == "" and app._backend.model == ""

    _run(scenario())


def test_typing_exact_parameter_context_opens_completion_automatically(
        tmp_path):
    async def scenario():
        adapter = _CompletionAdapter()
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("/", "m", "o", "d", "e", "l", "space")
            await _wait_until(
                lambda: isinstance(
                    app.screen, TUIRuntimeSettingPickerScreen), pilot)
            assert app.screen.completion_only is True
            assert len(adapter.model_reads) == 1
            assert adapter.state_reads == []
            assert adapter.model_writes == adapter.thinking_writes == []

    _run(scenario())


@pytest.mark.parametrize(("value", "expected", "kind"), (
    ("/model next", "/model openrouter/anthropic/claude-next", "model"),
    ("/thinking hi", "/thinking high", "thinking"),
))
def test_completion_selection_requires_a_second_enter_for_mutation(
        tmp_path, value, expected, kind):
    async def scenario():
        adapter = _CompletionAdapter(allow_writes=True)
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            composer, _screen = await _open_completion(app, pilot, value)
            await pilot.press("enter")
            await _wait_until(lambda: composer.value == expected, pilot)
            assert adapter.model_writes == adapter.thinking_writes == []

            await pilot.press("enter")
            writes = (adapter.model_writes if kind == "model"
                      else adapter.thinking_writes)
            await _wait_until(lambda: len(writes) == 1, pilot)
            assert app._backend.turn_calls == []
            if kind == "model":
                assert len(adapter.model_reads) == 3
                assert len(adapter.state_reads) == 1
            else:
                assert len(adapter.level_reads) == 2

    _run(scenario())


def test_failed_snapshot_is_not_retried_until_context_changes(tmp_path):
    async def scenario():
        adapter = _CompletionAdapter()
        adapter.model_error = RuntimeError("catalog unavailable")
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            value = "/model next"
            assert app._runtime_completion_input(value, "explicit") is True
            await _wait_until(
                lambda: not app._runtime_completion_pending, pilot)
            assert len(adapter.model_reads) == 1

            assert app._runtime_completion_input(value, "explicit") is True
            await pilot.pause(0.05)
            assert len(adapter.model_reads) == 1

            assert app._runtime_completion_input("/status", "context") is False
            assert app._runtime_completion_input(value, "explicit") is True
            await _wait_until(lambda: len(adapter.model_reads) == 2, pilot)

    _run(scenario())


def test_fast_query_changes_coalesce_behind_one_snapshot(tmp_path):
    async def scenario():
        adapter = _CompletionAdapter()
        adapter.model_gate = threading.Event()
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            for value in ("/model a", "/model ab", "/model next"):
                composer.value = value
                composer.cursor_position = len(value)
                assert app._runtime_completion_input(value, "typing") is True
            await _wait_until(adapter.model_started.is_set, pilot)
            assert len(adapter.model_reads) == 1

            adapter.model_gate.set()
            await _wait_until(
                lambda: isinstance(
                    app.screen, TUIRuntimeSettingPickerScreen), pilot)
            assert app.screen._query_text == "next"
            assert len(adapter.model_reads) == 1

    _run(scenario())


def test_late_snapshot_is_discarded_after_control_invalidation(tmp_path):
    async def scenario():
        adapter = _CompletionAdapter()
        adapter.model_gate = threading.Event()
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "/model next"
            composer.cursor_position = len(composer.value)
            assert app._runtime_completion_input(
                composer.value, "explicit") is True
            await _wait_until(adapter.model_started.is_set, pilot)

            app._invalidate_session_controls()
            adapter.model_gate.set()
            await _wait_until(adapter.model_finished.is_set, pilot)
            await pilot.pause(0.05)

            assert app._runtime_completion_choices is None
            assert not isinstance(
                app.screen, TUIRuntimeSettingPickerScreen)
            assert adapter.model_writes == adapter.thinking_writes == []

    _run(scenario())


@pytest.mark.parametrize("unsafe", ("bad'name", 'bad"name', "bad\\name"))
def test_malicious_model_candidate_cannot_break_slash_grammar(unsafe):
    action = RuntimeSettingPickerAction(
        "complete", "model", provider="hostile", model_id=unsafe)
    with pytest.raises(ValueError):
        _runtime_completion_value("model", action)


def test_context_parser_and_snapshot_sanitizer_fail_closed():
    assert parse_runtime_completion_context("/model open router") is not None
    assert parse_runtime_completion_context("/Model open") is None
    assert parse_runtime_completion_context("/model\nnext") is None
    assert parse_runtime_completion_context("/model " + "x" * 5000) is None

    models = RuntimeModelCatalog("ses_completion", (
        RuntimeModelRef("safe", "nested/model", "Nested"),
        RuntimeModelRef("a/b", "c", "collision one"),
        RuntimeModelRef("a", "b/c", "collision two"),
        RuntimeModelRef("unsafe", "bad name", "unsafe"),
        *tuple(RuntimeModelRef("provider", f"model-{index}")
               for index in range(300)),
    ))
    bounded = _bounded_completion_choices("model", models)
    identities = tuple(
        f"{model.provider}/{model.model_id}" for model in bounded.models)
    assert identities[0] == "safe/nested/model"
    assert "a/b/c" not in identities
    assert all("bad name" not in identity for identity in identities)
    assert len(identities) == 256

    levels = _bounded_completion_choices(
        "thinking", RuntimeThinkingLevels(
            "ses_completion", ("high", "HIGH", "max", "bad level")))
    assert levels.levels == ("max",)
