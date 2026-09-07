"""Local, side-effect-free slash completion for the TUI composer."""

from __future__ import annotations

from typing import Callable, Optional

from textual import events
from textual.binding import Binding
from textual.suggester import Suggester
from textual.widgets import Input

from .tui_command_specs import (
    command_spec,
    command_spellings,
    palette_callback,
    palette_specs,
)


_MAX_COMPLETION_INPUT_BYTES = 4096


class SlashCommandSuggester(Suggester):
    """Complete local command names without consulting a runtime or filesystem."""

    def __init__(self) -> None:
        super().__init__(use_cache=True, case_sensitive=False)
        self._commands = command_spellings()

    async def get_suggestion(self, value: str) -> str | None:
        return self.suggest(value)

    def suggest(self, value: str) -> str | None:
        if not _completion_context(value):
            return None
        prefix = value[1:].casefold()
        matches = tuple(name for name in self._commands
                        if name.startswith(prefix))
        if not matches:
            return None
        if prefix in matches:
            return f"/{prefix} "
        common = _common_prefix(matches)
        if len(common) <= len(prefix):
            return None
        suffix = " " if len(matches) == 1 else ""
        return f"/{common}{suffix}"


class SlashComposer(Input):
    """Input whose completion acceptance never submits in the same keystroke."""

    BINDINGS = [
        Binding("tab", "accept_slash_completion", show=False),
        Binding("escape", "dismiss_slash_completion", show=False),
        *Input.BINDINGS,
    ]

    def __init__(
            self, *args,
            dynamic_complete: Optional[Callable[[str, str], bool]] = None,
            **kwargs) -> None:
        self._slash_suggester = SlashCommandSuggester()
        self._dynamic_complete = dynamic_complete
        kwargs["suggester"] = self._slash_suggester
        super().__init__(*args, **kwargs)

    def action_accept_slash_completion(self) -> None:
        if not self._accept_slash_completion():
            self._request_dynamic_completion("explicit")

    def action_dismiss_slash_completion(self) -> None:
        self._suggestion = ""
        self._call_dynamic_completion("", "context")

    def action_cursor_right(self, select: bool = False) -> None:
        if not select and self._accept_slash_completion():
            return
        super().action_cursor_right(select)

    async def action_submit(self) -> None:
        if not _is_exact_command(self.value) and self._accept_slash_completion():
            return
        self._call_dynamic_completion("", "context")
        await super().action_submit()

    async def _on_key(self, event: events.Key) -> None:
        before = self.value
        await super()._on_key(event)
        if event.is_printable and self.value != before:
            self._request_dynamic_completion("typing")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is self:
            self._call_dynamic_completion(event.value, "context")

    def _accept_slash_completion(self) -> bool:
        if not self.cursor_at_end:
            return False
        suggestion = self._slash_suggester.suggest(self.value)
        if not _extends_value(self.value, suggestion):
            return False
        self.value = suggestion or self.value
        self.cursor_position = len(self.value)
        return True

    def _request_dynamic_completion(self, trigger: str) -> bool:
        if not self.cursor_at_end:
            return False
        return self._call_dynamic_completion(self.value, trigger)

    def _call_dynamic_completion(self, value: str, trigger: str) -> bool:
        callback = self._dynamic_complete
        if callback is None:
            return False
        try:
            return bool(callback(value, trigger))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False


def _completion_context(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("/"):
        return False
    try:
        if len(value.encode("utf-8")) > _MAX_COMPLETION_INPUT_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    return not any(char.isspace() for char in value[1:])


def _common_prefix(values: tuple[str, ...]) -> str:
    if not values:
        return ""
    first, last = min(values), max(values)
    index = 0
    while index < min(len(first), len(last)) and first[index] == last[index]:
        index += 1
    return first[:index]


def _extends_value(value: str, suggestion: str | None) -> bool:
    return bool(
        suggestion and len(suggestion) > len(value)
        and suggestion.casefold().startswith(value.casefold()))


def _is_exact_command(value: str) -> bool:
    return bool(
        _completion_context(value) and command_spec(value[1:]) is not None)
