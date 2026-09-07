"""Single-source slash command and side-effect-free completion contracts."""

from __future__ import annotations

import asyncio
import queue

from textual.widgets import Input, Markdown

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_command_specs import (
    COMMAND_SPECS,
    COMPACTION_BUSY,
    REVIEW_BUSY,
    SETTING_BUSY,
    TURN_BUSY,
    command_allowed_while,
    command_spec,
    command_spellings,
    palette_specs,
)
from pbatch.tui_composer import SlashCommandSuggester, SlashComposer
from pbatch.tui_model_picker import TUIRuntimeSettingPickerScreen
from pbatch.tui_protocol import RuntimeCapabilities
from pbatch.tui_runtime import (
    RuntimeInfo,
    RuntimeModelCatalog,
    RuntimeModelRef,
    RuntimeSession,
    RuntimeSessionState,
)
from pbatch.tui_shell import SLASH_HELP, parse_slash_command


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
        return {"status": "idle", "session_id": "ses_commands"}


class _OldGlobalPiAdapter:
    """Advertise the old unsafe setter while counting all control calls."""

    def __init__(self) -> None:
        capabilities = RuntimeCapabilities(runtime="pi-rpc", capabilities={
            "structured_events": True,
            "message_stream": True,
            "interrupt": True,
            "session_state": True,
            "model_catalog": True,
            "model_select": True,
        })
        self.info = RuntimeInfo(
            "pi-rpc", 1, capabilities, runtime_version="0.84.2",
            settings_scope="global")
        self.events_queue: queue.Queue = queue.Queue()
        self.control_calls: list[str] = []
        self.session_id = "ses_commands"

    def runtime_info(self):
        return self.info

    def initialize(self, _request):
        return self.info

    def start_session(self, request):
        return RuntimeSession(
            self.session_id, "/sessions/commands.jsonl",
            str(request.get("workspace_id", "")),
            "2026-08-26T00:00:00Z", "ACTIVE")

    def events(self):
        return self.events_queue

    def get_session_state(self, _request):
        self.control_calls.append("get_session_state")
        return RuntimeSessionState(
            self.session_id, "/sessions/commands.jsonl",
            model=RuntimeModelRef("anthropic", "claude-current"),
            thinking_level="off", is_streaming=False, is_compacting=False,
            pending_message_count=0)

    def get_available_models(self, _request):
        self.control_calls.append("get_available_models")
        return RuntimeModelCatalog(self.session_id, (
            RuntimeModelRef("anthropic", "claude-current"),
            RuntimeModelRef("openrouter", "anthropic/claude-next"),
        ))

    def set_model(self, _request):
        self.control_calls.append("set_model")
        raise AssertionError("unsafe global setter must not be called")

    def close(self):
        self.events_queue.put(None)


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


def test_registry_is_unique_and_drives_help_dispatch_and_palette():
    spellings = command_spellings()
    assert len(spellings) == len(set(spellings))
    assert "models" not in spellings and "scoped-models" not in spellings
    assert command_spec("runs") is command_spec("agents")
    assert command_spec("artifact") is command_spec("artifacts")

    palette = palette_specs()
    assert {spec.name for spec in palette} >= {
        "session", "model", "thinking"}
    assert "compact" not in {spec.name for spec in palette}
    for spec in COMMAND_SPECS:
        assert callable(getattr(PiBatchTUI, spec.handler_name, None))
        assert f"`/{spec.usage}`" in SLASH_HELP
        for spelling in spec.spellings:
            parsed = parse_slash_command(f"/{spelling}")
            assert parsed is not None and command_spec(parsed.name) is spec
    for spec in palette:
        assert callable(getattr(PiBatchTUI, spec.palette_handler, None))


def test_busy_allowlists_are_alias_aware_and_unchanged():
    allowed = lambda state: {
        name for name in command_spellings()
        if command_allowed_while(name, state)}
    assert allowed(SETTING_BUSY) == {"help", "quit", "status"}
    assert allowed(COMPACTION_BUSY) == {
        "compact", "help", "quit", "session", "status"}
    assert allowed(REVIEW_BUSY) == {
        "agents", "approvals", "artifact", "artifacts", "completion",
        "findings", "runs", "session"}
    assert allowed(TURN_BUSY) == {
        "actor-steer", "actor-stop", "agents", "approvals", "artifact",
        "artifacts", "completion", "findings", "queue", "runs", "session",
        "steer"}


def test_static_suggester_is_bounded_prefix_only_and_argument_free():
    suggester = SlashCommandSuggester()
    assert suggester.suggest("/thi") == "/thinking "
    assert suggester.suggest("/MODEL") == "/model "
    assert suggester.suggest("/artifact") == "/artifact "
    assert suggester.suggest("/") is None
    assert suggester.suggest("/model ") is None
    assert suggester.suggest("prefix /model") is None
    assert suggester.suggest("/definitely-unknown") is None
    assert suggester.suggest("/" + ("m" * 5000)) is None


def test_partial_enter_completes_before_exact_enter_submits(tmp_path):
    async def scenario():
        backend = _Backend(str(tmp_path))
        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path), agent="not-pi", runtime="legacy"),
            backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", SlashComposer)
            before = app.query_one("#conversation", Markdown).source
            composer.value = "/sta"
            composer.cursor_position = len(composer.value)
            await pilot.press("enter")
            assert composer.value == "/status "
            assert app.query_one("#conversation", Markdown).source == before
            assert backend.turn_calls == []

            await pilot.press("enter")
            await pilot.pause()
            assert composer.value == ""
            assert '"session_id": "ses_commands"' in app.query_one(
                "#conversation", Markdown).source
            assert backend.turn_calls == []

    _run(scenario())


def test_old_global_pi_tab_completion_has_zero_runtime_side_effects(tmp_path):
    async def scenario():
        backend = _Backend(str(tmp_path))
        adapter = _OldGlobalPiAdapter()
        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path), agent="pi", runtime="pi-rpc"),
            backend=backend, pi_rpc_factory=lambda _cwd, **_kwargs: adapter)
        async with app.run_test(size=(100, 30)) as pilot:
            composer = app.query_one("#composer", Input)
            before = app.query_one("#conversation", Markdown).source
            composer.value = "/mo"
            composer.cursor_position = len(composer.value)
            await pilot.press("tab")
            assert composer.value == "/model "
            assert adapter.control_calls == []
            assert backend.turn_calls == []
            assert app.options.model == "" and backend.model == ""
            assert app.query_one("#conversation", Markdown).source == before

            await pilot.press("enter")
            await _wait_until(
                lambda: isinstance(app.screen, TUIRuntimeSettingPickerScreen),
                pilot)
            assert adapter.control_calls == [
                "get_session_state", "get_available_models"]
            assert app.screen.can_apply is False
            assert "set_model" not in adapter.control_calls
            await pilot.press("escape")

    _run(scenario())


def test_palette_exposes_model_thinking_and_session_without_lossy_compact(
        tmp_path):
    async def scenario():
        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path), agent="not-pi", runtime="legacy"),
            backend=_Backend(str(tmp_path)))
        async with app.run_test(size=(100, 30)):
            titles = {
                command.title for command in app.get_system_commands(app.screen)}
            assert {"Runtime session", "Runtime model", "Thinking level"} <= titles
            assert "Compact session" not in titles

    _run(scenario())
