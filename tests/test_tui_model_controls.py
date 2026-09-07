"""Failure-first contracts for active-session model and thinking controls."""

from __future__ import annotations

import asyncio
import queue
import threading

from textual.widgets import Input, Markdown

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_model_controls import (
    _find_exact_model,
    render_model_catalog,
    render_thinking_levels,
)
from pbatch.tui_model_picker import TUIRuntimeSettingPickerScreen
from pbatch.tui_protocol import RuntimeCapabilities
from pbatch.tui_runtime import (
    RuntimeInfo,
    RuntimeModelCatalog,
    RuntimeModelRef,
    RuntimeSession,
    RuntimeSessionState,
    RuntimeThinkingLevels,
)
from pbatch.tui_view_support import is_pi_binary


class ModelControlAdapter:
    """Typed adapter with controllable catalog latency and readbacks."""

    def __init__(self, *, capabilities=()) -> None:
        enabled = {name: True for name in capabilities}
        self.info = RuntimeInfo(
            "pi-rpc", 1,
            RuntimeCapabilities(runtime="pi-rpc", capabilities=enabled),
            runtime_version="0.84.3", settings_scope="session",
        )
        self.session_id = "ses_models"
        self.provider_session_id = "/sessions/models.jsonl"
        self.state = _state(self.session_id, "anthropic", "claude-current")
        self.catalog = RuntimeModelCatalog(self.session_id, (
            RuntimeModelRef("anthropic", "claude-current", "Current"),
            RuntimeModelRef("openrouter", "anthropic/claude-next", "Next"),
        ))
        self.levels = RuntimeThinkingLevels(
            self.session_id, ("off", "high", "max"))
        self.model_readback = _state(
            self.session_id, "openrouter", "anthropic/claude-next",
            thinking="high")
        self.thinking_readback = _state(
            self.session_id, "anthropic", "claude-current", thinking="high")
        self.state_calls = []
        self.catalog_calls = []
        self.level_calls = []
        self.level_error = None
        self.model_calls = []
        self.thinking_calls = []
        self.start_calls = []
        self.close_calls = 0
        self.catalog_started = threading.Event()
        self.catalog_gate: threading.Event | None = None
        self.events_queue: queue.Queue = queue.Queue()

    def runtime_info(self):
        return self.info

    def initialize(self, _request):
        return self.info

    def start_session(self, request):
        self.start_calls.append(request)
        return RuntimeSession(
            self.session_id, self.provider_session_id,
            str(request.get("workspace_id", "")),
            "2026-08-26T00:00:00Z", "ACTIVE")

    def events(self):
        return self.events_queue

    def get_session_state(self, request):
        self.state_calls.append(request)
        return self.state

    def get_available_models(self, request):
        self.catalog_calls.append(request)
        self.catalog_started.set()
        if self.catalog_gate is not None:
            self.catalog_gate.wait(timeout=2)
        return self.catalog

    def get_available_thinking_levels(self, request):
        self.level_calls.append(request)
        if self.level_error is not None:
            raise self.level_error
        return self.levels

    def set_model(self, request):
        self.model_calls.append(request)
        return self.model_readback

    def set_thinking_level(self, request):
        self.thinking_calls.append(request)
        return self.thinking_readback

    def close(self):
        self.close_calls += 1
        if self.catalog_gate is not None:
            self.catalog_gate.set()
        self.events_queue.put(None)


class ControlBackend:
    agent = "pi"
    provider = ""
    model = ""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.turn_calls = []

    def set_emit(self, _emit) -> None:
        return None

    def run_turn(self, prompt):
        self.turn_calls.append(prompt)
        return {"success": True, "stdout": "", "stderr": ""}

    def snapshot(self):
        return {"status": "idle", "session_id": "ses_models"}


class LegacyBackend(ControlBackend):
    agent = "legacy-agent"

    def __init__(self, cwd: str) -> None:
        super().__init__(cwd)
        self.provider = ""
        self.model = "old-default"
        self.memory_mode = "off"


def _state(
        session_id: str, provider: str, model_id: str,
        *, thinking: str = "off") -> RuntimeSessionState:
    return RuntimeSessionState(
        session_id, "/sessions/models.jsonl", name="models",
        model=RuntimeModelRef(provider, model_id),
        thinking_level=thinking, is_streaming=False, is_compacting=False,
        pending_message_count=0)


def _app(tmp_path, adapter: ModelControlAdapter) -> PiBatchTUI:
    return PiBatchTUI(
        TuiOptions(cwd=str(tmp_path), agent="pi", runtime="pi-rpc"),
        backend=ControlBackend(str(tmp_path)),
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


def test_model_and_thinking_renderers_strip_controls_and_hide_provider_path():
    hostile = "\x1b[31m\x07model`name" + ("x" * 2_000)
    models = tuple(
        RuntimeModelRef(f"provider-{index}", hostile, hostile)
        for index in range(60))
    state = RuntimeSessionState(
        "ses_models", "/must/not/render.jsonl",
        model=RuntimeModelRef("provider-0", hostile),
        thinking_level="\x07high")

    catalog_text = render_model_catalog(
        RuntimeModelCatalog("ses_models", models), state, width=48)
    thinking_text = render_thinking_levels(
        RuntimeThinkingLevels("ses_models", (hostile,)), state)

    for rendered in (catalog_text, thinking_text):
        assert "\x1b" not in rendered and "\x07" not in rendered
        assert "/must/not/render.jsonl" not in rendered
    assert catalog_text.count("\n- `") == 48
    assert "12 more models omitted" in catalog_text


def test_default_tui_pi_override_is_one_trusted_executable(monkeypatch, tmp_path):
    monkeypatch.setenv("PBATCH_PI_BIN", "/path with spaces/pi-edge")
    app = PiBatchTUI(
        TuiOptions(cwd=str(tmp_path)), backend=ControlBackend(str(tmp_path)))
    assert app._pi_runtime_agent("pi") == (
        "/path with spaces/pi-edge", True)

    explicit = PiBatchTUI(
        TuiOptions(cwd=str(tmp_path), agent="pi"),
        backend=ControlBackend(str(tmp_path)))
    assert explicit._pi_runtime_agent("pi") == ("pi", True)
    assert is_pi_binary("pi -ne") is False


def test_thinking_renderer_enforces_byte_boundary():
    hostile = "thinking-" * 100
    state = _state("ses_models", "provider", "model", thinking=hostile)
    rendered = render_thinking_levels(
        RuntimeThinkingLevels("ses_models", (hostile,) * 1_000), state)
    assert len(rendered.encode("utf-8")) <= 32 * 1024


def test_model_catalog_respects_requested_line_width():
    hostile = "model-" * 100
    state = _state("ses_models", "provider", hostile)
    rendered = render_model_catalog(RuntimeModelCatalog(
        "ses_models", (RuntimeModelRef("provider", hostile, hostile),)),
        state, width=48)
    assert all(len(line) <= 48 for line in rendered.splitlines())


def test_model_list_and_exact_selection_use_active_session_without_rebind(
        tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog", "model_select"))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            before = app.query_one("#conversation", Markdown).source
            await _submit(app, pilot, "/model")
            await _wait_until(
                lambda: isinstance(app.screen, TUIRuntimeSettingPickerScreen),
                pilot)
            assert app.query_one("#conversation", Markdown).source == before
            assert app.screen.kind == "model" and app.screen.can_apply is True
            assert len(adapter.state_calls) == len(adapter.catalog_calls) == 1

            await pilot.press("down", "enter")
            await _wait_until(lambda: len(adapter.model_calls) == 1, pilot)
            await _wait_until(
                lambda: "Model updated" in app.query_one(
                    "#conversation", Markdown).source, pilot)
            request = adapter.model_calls[0]
            assert request.provider == "openrouter"
            assert request.model_id == "anthropic/claude-next"
            assert request.session_id == "ses_models"
            assert len(adapter.start_calls) == 1
            assert adapter.close_calls == 0
            assert app.options.model == "" and app._backend.model == ""

    _run(scenario())


def test_model_query_resolves_case_insensitive_unique_bare_id(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog", "model_select"))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(app, pilot, "/model ANTHROPIC/CLAUDE-NEXT")
            await _wait_until(lambda: len(adapter.model_calls) == 1, pilot)
            request = adapter.model_calls[0]
            assert (request.provider, request.model_id) == (
                "openrouter", "anthropic/claude-next")
            assert not isinstance(app.screen, TUIRuntimeSettingPickerScreen)

    _run(scenario())


def test_exact_bare_model_id_is_ambiguous_across_providers():
    catalog = RuntimeModelCatalog("ses_models", (
        RuntimeModelRef("one", "shared", "One"),
        RuntimeModelRef("two", "shared", "Two"),
    ))
    assert _find_exact_model("shared", catalog) is None
    assert _find_exact_model("TWO/SHARED", catalog) == catalog.models[1]


def test_old_pi_capability_false_never_sets_or_rebinds(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog"))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(app, pilot, "/model")
            await _wait_until(
                lambda: isinstance(app.screen, TUIRuntimeSettingPickerScreen),
                pilot)
            assert app.screen.can_apply is False
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, TUIRuntimeSettingPickerScreen)
            assert adapter.model_calls == []
            assert len(adapter.catalog_calls) == 1
            assert len(adapter.start_calls) == 1
            assert adapter.close_calls == 0
            assert app.options.model == "" and app._backend.model == ""
            await pilot.press("escape")

    _run(scenario())


def test_global_settings_scope_is_read_only_even_with_select_capability(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog", "model_select"))
        adapter.info = RuntimeInfo(
            "pi-rpc", 1, adapter.info.capabilities,
            runtime_version="0.84.1", settings_scope="global")
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(app, pilot, "/model")
            await _wait_until(
                lambda: isinstance(app.screen, TUIRuntimeSettingPickerScreen),
                pilot)
            assert app.screen.can_apply is False
            await pilot.press("down", "enter")
            await pilot.pause()
            assert isinstance(app.screen, TUIRuntimeSettingPickerScreen)
            assert adapter.model_calls == []
            await pilot.press("escape")

    _run(scenario())


def test_current_picker_selection_is_a_noop(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog", "model_select"))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(app, pilot, "/model")
            await _wait_until(
                lambda: isinstance(app.screen, TUIRuntimeSettingPickerScreen),
                pilot)
            await pilot.press("enter")
            await _wait_until(
                lambda: not isinstance(
                    app.screen, TUIRuntimeSettingPickerScreen), pilot)
            assert adapter.model_calls == []
            assert app._last_status == "Runtime setting unchanged"

    _run(scenario())


def test_open_picker_blocks_turn_and_session_replacement(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog", "model_select"))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(app, pilot, "/model")
            await _wait_until(
                lambda: isinstance(app.screen, TUIRuntimeSettingPickerScreen),
                pilot)
            assert app._start_turn("must not start") is False
            assert app._backend.turn_calls == []
            app._new_session()
            assert len(adapter.start_calls) == 1
            assert app._runtime_setting_change_running() is True
            await pilot.press("escape")
            await _wait_until(
                lambda: not app._runtime_setting_change_running(), pilot)

    _run(scenario())


def test_thinking_uses_dynamic_levels_and_renders_clamped_readback(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "thinking_levels", "thinking_select"))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(app, pilot, "/thinking")
            await _wait_until(
                lambda: isinstance(app.screen, TUIRuntimeSettingPickerScreen),
                pilot)
            assert app.screen.kind == "thinking"
            assert tuple(item.level for item in app.screen._visible) == (
                "off", "high", "max")
            await pilot.press("escape")

            await _submit(app, pilot, "/thinking MAX")
            await _wait_until(lambda: len(adapter.thinking_calls) == 1, pilot)
            await _wait_until(
                lambda: "Requested: `max`" in app.query_one(
                    "#conversation", Markdown).source, pilot)
            source = app.query_one("#conversation", Markdown).source
            assert "Effective: `high`" in source
            request = adapter.thinking_calls[0]
            assert request.level == "max" and request.session_id == "ses_models"

            await _submit(app, pilot, "/thinking medium")
            await _wait_until(lambda: len(adapter.level_calls) == 3, pilot)
            assert len(adapter.thinking_calls) == 1
            assert "unknown thinking level" in app.query_one(
                "#conversation", Markdown).source

    _run(scenario())


def test_pending_model_change_rejects_reentry(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog", "model_select"))
        adapter.catalog_gate = threading.Event()
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(
                app, pilot, "/model openrouter/anthropic/claude-next")
            await _wait_until(adapter.catalog_started.is_set, pilot)
            await _submit(app, pilot, "/model anthropic/claude-current")
            assert len(adapter.catalog_calls) == 1
            assert adapter.model_calls == []

            adapter.catalog_gate.set()
            await _wait_until(lambda: len(adapter.model_calls) == 1, pilot)
            assert adapter.model_calls[0].provider == "openrouter"

    _run(scenario())


def test_late_model_query_is_discarded_after_adapter_boundary_change(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog"))
        adapter.catalog_gate = threading.Event()
        replacement = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog"))
        replacement.session_id = "ses_replacement"
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            before = app.query_one("#conversation", Markdown).source
            await _submit(app, pilot, "/model")
            await _wait_until(adapter.catalog_started.is_set, pilot)
            app._adapter = replacement
            app._adapter_session_id = "ses_replacement"
            adapter.catalog_gate.set()
            await pilot.pause(0.1)
            assert app.query_one("#conversation", Markdown).source == before
            adapter.close()

    _run(scenario())


def test_picker_selection_is_discarded_after_adapter_boundary_change(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog", "model_select"))
        replacement = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog", "model_select"))
        replacement.session_id = "ses_replacement"
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(app, pilot, "/model")
            await _wait_until(
                lambda: isinstance(app.screen, TUIRuntimeSettingPickerScreen),
                pilot)
            app._adapter = replacement
            app._adapter_session_id = "ses_replacement"
            await pilot.press("down", "enter")
            await pilot.pause()
            assert adapter.model_calls == []
            assert replacement.model_calls == []
            adapter.close()

    _run(scenario())


def test_picker_selection_is_revalidated_against_fresh_catalog(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog", "model_select"))
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(app, pilot, "/model")
            await _wait_until(
                lambda: isinstance(app.screen, TUIRuntimeSettingPickerScreen),
                pilot)
            adapter.catalog = RuntimeModelCatalog(
                adapter.session_id, (adapter.catalog.models[0],))
            await pilot.press("down", "enter")
            await _wait_until(lambda: len(adapter.catalog_calls) == 2, pilot)
            assert adapter.model_calls == []
            await _wait_until(
                lambda: "provider/model is not available" in app.query_one(
                    "#conversation", Markdown).source, pilot)

    _run(scenario())


def test_confirmed_model_change_survives_thinking_refresh_failure(tmp_path):
    async def scenario():
        adapter = ModelControlAdapter(capabilities=(
            "session_state", "model_catalog", "model_select"))
        adapter.level_error = RuntimeError("thinking refresh unavailable")
        app = _app(tmp_path, adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(
                app, pilot, "/model openrouter/anthropic/claude-next")
            await _wait_until(lambda: len(adapter.model_calls) == 1, pilot)
            await _wait_until(
                lambda: "Model updated" in app.query_one(
                    "#conversation", Markdown).source, pilot)
            source = app.query_one("#conversation", Markdown).source
            assert "thinking choices could not be refreshed" in source
            assert "thinking refresh unavailable" in source

    _run(scenario())


def test_legacy_model_command_still_updates_configuration(tmp_path):
    async def scenario():
        backend = LegacyBackend(str(tmp_path))
        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path), runtime="legacy"), backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await _submit(app, pilot, "/model next-default")
            assert backend.model == "next-default"
            assert app.options.model == "next-default"
            assert "`model` set to `next-default`" in app.query_one(
                "#conversation", Markdown).source

    _run(scenario())
