"""Queued input changes must not invalidate a newer completion context."""

import asyncio

import pytest
from textual.app import App
from textual.widgets import Input

from pbatch.tui_composer import SlashComposer


class ComposerHost(App):
    def __init__(self):
        super().__init__()
        self.calls = []

    def compose(self):
        yield SlashComposer(id="composer", dynamic_complete=self.record)

    def record(self, value, trigger):
        self.calls.append((value, trigger))
        return True


def _check_composer(value, check):
    async def scenario():
        app = ComposerHost()
        async with app.run_test() as pilot:
            composer = app.query_one("#composer", SlashComposer)
            composer.value = value
            await pilot.pause()
            app.calls.clear()
            check(composer, app.calls)

    asyncio.run(scenario())


@pytest.mark.parametrize("stale_value", (
    "/model a", "/thinking high", "/status", "",
))
def test_stale_input_change_does_not_reach_runtime_completion(stale_value):
    def check(composer, calls):
        composer.on_input_changed(Input.Changed(composer, stale_value))
        assert calls == []
        assert composer.value == "/model next"

    _check_composer("/model next", check)


@pytest.mark.parametrize("value", ("/model next", "/thinking high", "/status", ""))
def test_current_input_change_still_updates_runtime_context(value):
    def check(composer, calls):
        composer.on_input_changed(Input.Changed(composer, value))
        assert calls == [(value, "context")]

    _check_composer(value, check)


def test_another_input_cannot_change_the_composer_context():
    def check(composer, calls):
        other = Input(value="/model next")
        composer.on_input_changed(Input.Changed(other, "/model next"))
        assert calls == []

    _check_composer("/model next", check)
