"""Pure Textual contracts for the local model and thinking picker."""

from __future__ import annotations

import asyncio

import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, OptionList, Static

from pbatch.tui_model_picker import (
    RuntimeSettingPickerAction,
    TUIRuntimeSettingPickerScreen,
)
from pbatch.tui_runtime import (
    RuntimeModelCatalog,
    RuntimeModelRef,
    RuntimeSessionState,
    RuntimeThinkingLevels,
)


class PickerHost(App[None]):
    """Host a picker while exposing a conflicting priority Ctrl+C binding."""

    BINDINGS = [
        Binding("ctrl+c", "parent_cancel", "Parent cancel", priority=True),
    ]

    def __init__(self, picker: TUIRuntimeSettingPickerScreen) -> None:
        super().__init__()
        self.picker = picker
        self.results = []
        self.parent_cancel_calls = 0

    def compose(self) -> ComposeResult:
        yield Static("host", id="host")

    def on_mount(self) -> None:
        self.push_screen(self.picker, self.results.append)

    def action_parent_cancel(self) -> None:
        self.parent_cancel_calls += 1


def _state(
        model: RuntimeModelRef | None = None, *, thinking: str = "off",
        session_id: str = "ses_picker") -> RuntimeSessionState:
    return RuntimeSessionState(
        session_id, "/provider/path/must-not-render.jsonl",
        model=model, thinking_level=thinking,
        is_streaming=False, is_compacting=False)


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


def test_model_picker_caps_matches_and_returns_unmodified_raw_identity():
    raw_id = "anthropic/\x1b[31mclaude\x07/next"
    current = RuntimeModelRef(
        "openrouter", raw_id, "[bold]名称[/bold]" + "界" * 300)
    models = tuple(RuntimeModelRef("provider", f"model-{index}", "Model")
                   for index in range(299)) + (current,)
    picker = TUIRuntimeSettingPickerScreen(
        "model", _state(RuntimeModelRef("openrouter", raw_id)),
        RuntimeModelCatalog("ses_picker", models))
    app = PickerHost(picker)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            options = picker.query_one("#runtime-setting-options", OptionList)
            assert options.option_count == 256
            assert options.highlighted == 0
            assert options._markup is False
            heading = str(picker.query_one(
                "#runtime-setting-heading", Static).content)
            assert "showing 256 of 300 matching" in heading

            label = str(options.get_option_at_index(0).prompt)
            assert "\x1b" not in label and "\x07" not in label
            assert "[bold]" in label
            assert cell_len(label) <= 120
            assert "/provider/path/must-not-render" not in picker._detail(0)

            await pilot.press("enter")
            await pilot.pause()
            assert app.results == [RuntimeSettingPickerAction(
                "unchanged", "model", provider="openrouter", model_id=raw_id)]

    _run(scenario())


def test_model_filter_is_local_case_insensitive_and_arrows_wrap():
    models = (
        RuntimeModelRef("anthropic", "alpha", "First"),
        RuntimeModelRef("openrouter", "vendor/beta", "Second Choice"),
        RuntimeModelRef("google", "gamma", "Third"),
    )
    picker = TUIRuntimeSettingPickerScreen(
        "model", _state(models[0]), RuntimeModelCatalog("ses_picker", models))
    app = PickerHost(picker)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            search = picker.query_one("#runtime-setting-search", Input)
            options = picker.query_one("#runtime-setting-options", OptionList)
            assert search.has_focus and options.highlighted == 0

            await pilot.press("up")
            assert options.highlighted == 2
            await pilot.press("down")
            assert options.highlighted == 0

            search.value = "ORVDBT"
            await pilot.pause()
            assert options.option_count == 1
            assert "vendor/beta" in str(options.get_option_at_index(0).prompt)
            assert search.has_focus

            await pilot.press("enter")
            await pilot.pause()
            assert app.results == [RuntimeSettingPickerAction(
                "select", "model", provider="openrouter",
                model_id="vendor/beta")]

    _run(scenario())


def test_thinking_picker_uses_dynamic_levels_and_description_filter():
    levels = RuntimeThinkingLevels(
        "ses_picker", ("off", "turbo-custom", "xhigh"))
    picker = TUIRuntimeSettingPickerScreen(
        "thinking", _state(thinking="xhigh"), levels)
    app = PickerHost(picker)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            options = picker.query_one("#runtime-setting-options", OptionList)
            assert options.option_count == 3
            labels = [str(options.get_option_at_index(index).prompt)
                      for index in range(3)]
            assert ["off" in labels[0], "turbo-custom" in labels[1],
                    "xhigh" in labels[2]] == [True, True, True]
            assert options.highlighted == 2

            search = picker.query_one("#runtime-setting-search", Input)
            search.value = "32K"
            await pilot.pause()
            assert options.option_count == 1
            assert "Extra-high reasoning" in str(
                options.get_option_at_index(0).prompt)

            search.value = "provider level"
            await pilot.pause()
            assert options.option_count == 1
            assert "turbo-custom" in str(options.get_option_at_index(0).prompt)

            await pilot.press("enter")
            await pilot.pause()
            assert app.results == [RuntimeSettingPickerAction(
                "select", "thinking", level="turbo-custom")]

    _run(scenario())


def test_read_only_and_empty_results_never_dismiss_or_return_selection():
    model = RuntimeModelRef("anthropic", "claude", "Claude")
    picker = TUIRuntimeSettingPickerScreen(
        "model", _state(model),
        RuntimeModelCatalog("ses_picker", (model,)), can_apply=False)
    app = PickerHost(picker)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            assert "read-only" in picker._heading()
            await pilot.press("enter")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.screen is picker and app.results == []

            search = picker.query_one("#runtime-setting-search", Input)
            search.value = "no-such-model"
            await pilot.pause()
            options = picker.query_one("#runtime-setting-options", OptionList)
            assert options.option_count == 0 and options.highlighted is None
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is picker and app.results == []

            await pilot.press("escape")
            await pilot.pause()
            assert app.results == [None]

    _run(scenario())


def test_completion_only_picker_tab_inserts_even_the_current_choice():
    model = RuntimeModelRef("anthropic", "claude", "Claude")
    picker = TUIRuntimeSettingPickerScreen(
        "model", _state(model),
        RuntimeModelCatalog("ses_picker", (model,)),
        can_apply=False, completion_only=True)
    app = PickerHost(picker)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            assert "completion" in picker._heading()
            assert "Tab/Enter insert" in picker._hint()
            assert "submit the command" in picker._detail(0)
            await pilot.press("tab")
            await pilot.pause()
            assert app.results == [RuntimeSettingPickerAction(
                "complete", "model", provider="anthropic",
                model_id="claude")]

    _run(scenario())


def test_completion_only_uses_pi_model_rank_and_level_only_search():
    models = (
        RuntimeModelRef("github-copilot", "gpt-5.2-codex", "Copilot"),
        RuntimeModelRef("openai-codex", "gpt-5.5", "OpenAI"),
    )
    picker = TUIRuntimeSettingPickerScreen(
        "model", _state(), RuntimeModelCatalog("ses_picker", models),
        initial_query="codexgpt", can_apply=False, completion_only=True)
    assert [(item.provider, item.model_id) for item in picker._visible] == [
        ("openai-codex", "gpt-5.5"),
        ("github-copilot", "gpt-5.2-codex"),
    ]
    assert picker._visible[0].completion_label == "gpt-5.5 — openai-codex"

    levels = RuntimeThinkingLevels("ses_picker", ("minimal", "high"))
    for description_only_query in ("brief", "deep"):
        thinking = TUIRuntimeSettingPickerScreen(
            "thinking", _state(), levels,
            initial_query=description_only_query,
            can_apply=False, completion_only=True)
        assert thinking._visible == ()


def test_ctrl_c_cancels_modal_before_conflicting_app_priority_binding():
    levels = RuntimeThinkingLevels("ses_picker", ("off", "high"))
    picker = TUIRuntimeSettingPickerScreen(
        "thinking", _state(thinking="off"), levels)
    app = PickerHost(picker)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.results == [None]
            assert app.parent_cancel_calls == 0

    _run(scenario())


def test_picker_rejects_wrong_kind_choice_type_and_session_scope():
    state = _state()
    models = RuntimeModelCatalog("ses_picker", ())
    levels = RuntimeThinkingLevels("ses_picker", ())

    with pytest.raises(ValueError, match="kind"):
        TUIRuntimeSettingPickerScreen("provider", state, models)
    with pytest.raises(TypeError, match="wrong runtime type"):
        TUIRuntimeSettingPickerScreen("model", state, levels)
    with pytest.raises(ValueError, match="do not match"):
        TUIRuntimeSettingPickerScreen(
            "thinking", state, RuntimeThinkingLevels("ses_other", ("off",)))
    with pytest.raises(TypeError, match="initial_query"):
        TUIRuntimeSettingPickerScreen(
            "model", state, models, initial_query=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="can_apply"):
        TUIRuntimeSettingPickerScreen(
            "thinking", state, levels, can_apply=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="completion_only"):
        TUIRuntimeSettingPickerScreen(
            "thinking", state, levels,
            completion_only=1)  # type: ignore[arg-type]
