"""Local, provider-neutral model and thinking pickers for the product TUI."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

from rich.cells import cell_len, set_cell_size
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .tui_runtime import (
    RuntimeModelCatalog,
    RuntimeModelRef,
    RuntimeSessionState,
    RuntimeThinkingLevels,
)
from .tui_view_support import clean_text, one_line


_MODEL_VISIBLE_LIMIT = 256
_LABEL_CELLS = 120
_SEARCH_CHARS = 1024
_QUERY_CHARS = 256
_THINKING_DESCRIPTIONS = {
    "off": "No reasoning",
    "minimal": "Very brief reasoning (~1k tokens)",
    "low": "Light reasoning (~2k tokens)",
    "medium": "Moderate reasoning (~8k tokens)",
    "high": "Deep reasoning (~16k tokens)",
    "xhigh": "Extra-high reasoning (~32k tokens)",
    "max": "Maximum reasoning",
}


@dataclass(frozen=True)
class RuntimeSettingPickerAction:
    """One typed picker selection returned to the owning application."""

    operation: str
    kind: str
    provider: str = ""
    model_id: str = ""
    level: str = ""


@dataclass(frozen=True)
class _PickerItem:
    provider: str = ""
    model_id: str = ""
    level: str = ""
    label: str = ""
    search: str = ""
    completion_label: str = ""
    completion_search: str = ""
    current: bool = False


class _PickerOptionList(OptionList):
    """Keep typing focus in the local filter while retaining mouse selection."""

    can_focus = False


class TUIRuntimeSettingPickerScreen(
        ModalScreen[Optional[RuntimeSettingPickerAction]]):
    """Filter a frozen provider snapshot without performing provider I/O."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", priority=True, show=False),
        Binding("ctrl+s", "persistent_unavailable", "Session only",
                priority=True, show=False),
        Binding("up", "move(-1)", "Previous", priority=True, show=False),
        Binding("down", "move(1)", "Next", priority=True, show=False),
        Binding("enter", "choose", "Apply", priority=True, show=False),
        Binding("tab", "accept_completion", "Complete",
                priority=True, show=False),
    ]
    CSS = """
    TUIRuntimeSettingPickerScreen {
        align: center middle;
        background: $background;
    }
    #runtime-setting-dialog {
        width: 88%; height: 84%; min-width: 38;
        background: $surface; border: round $accent;
    }
    #runtime-setting-heading {
        height: 3; padding: 1 2; text-style: bold; background: $boost;
    }
    #runtime-setting-search { height: 3; margin: 0 1; }
    #runtime-setting-options { height: 1fr; padding: 0 1; }
    #runtime-setting-detail {
        height: 4; padding: 0 2; color: $text-muted;
        border-top: solid $panel-lighten-1;
    }
    #runtime-setting-hint { height: 3; padding: 0 2; color: $text-muted; }
    """

    def __init__(
            self, kind: str, state: RuntimeSessionState,
            choices: RuntimeModelCatalog | RuntimeThinkingLevels, *,
            initial_query: str = "", can_apply: bool = True,
            completion_only: bool = False) -> None:
        super().__init__(id="runtime-setting-picker")
        if kind not in {"model", "thinking"}:
            raise ValueError("kind must be 'model' or 'thinking'")
        if not isinstance(state, RuntimeSessionState):
            raise TypeError("state must be a RuntimeSessionState")
        if not isinstance(can_apply, bool):
            raise TypeError("can_apply must be a boolean")
        if not isinstance(completion_only, bool):
            raise TypeError("completion_only must be a boolean")
        self.kind = kind
        self.state = state
        self.choices = choices
        self.can_apply = can_apply
        self.completion_only = completion_only
        self._query_text = _query_text(initial_query)
        self._items = _picker_items(kind, state, choices)
        self._visible, self._matching_count = _filter_items(
            kind, self._items, self._query_text,
            pi_compatible=self.completion_only)

    def compose(self) -> ComposeResult:
        noun = "models" if self.kind == "model" else "thinking levels"
        with Vertical(id="runtime-setting-dialog"):
            yield Static(self._heading(), id="runtime-setting-heading", markup=False)
            yield Input(
                value=self._query_text, placeholder=f"Filter {noun}…",
                max_length=_QUERY_CHARS, id="runtime-setting-search")
            yield _PickerOptionList(
                *self._options(), id="runtime-setting-options", markup=False)
            yield Static(self._detail(0), id="runtime-setting-detail", markup=False)
            yield Static(
                self._hint(),
                id="runtime-setting-hint", markup=False)

    def on_mount(self) -> None:
        self._select_initial()
        self.query_one("#runtime-setting-search", Input).focus()

    def check_consume_key(self, key: str, character: Optional[str]) -> bool:
        """Keep the owning App's priority Ctrl+C behind this modal."""
        return key == "ctrl+c" or super().check_consume_key(key, character)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "runtime-setting-search":
            return
        self._query_text = _query_text(event.value)
        self._visible, self._matching_count = _filter_items(
            self.kind, self._items, self._query_text,
            pi_compatible=self.completion_only)
        picker = self.query_one("#runtime-setting-options", OptionList)
        picker.set_options(self._options())
        self._select_initial()
        self.query_one("#runtime-setting-heading", Static).update(self._heading())

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        self._update_detail(event.option_index)

    def on_option_list_option_selected(
            self, event: OptionList.OptionSelected) -> None:
        self._choose_index(event.option_index)

    def action_move(self, offset: int) -> None:
        if not self._visible:
            return
        picker = self.query_one("#runtime-setting-options", OptionList)
        current = picker.highlighted
        if current is None:
            current = 0 if offset >= 0 else len(self._visible) - 1
        else:
            current = (current + offset) % len(self._visible)
        picker.highlighted = current
        self._update_detail(current)

    def action_choose(self) -> None:
        picker = self.query_one("#runtime-setting-options", OptionList)
        self._choose_index(picker.highlighted)

    def action_accept_completion(self) -> None:
        if self.completion_only:
            self.action_choose()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_persistent_unavailable(self) -> None:
        if self.completion_only:
            self.notify(
                "Completion only inserts text; submit the command to apply it.",
                severity="warning")
            return
        self.notify(
            "Persistent model settings are unavailable here; Enter applies "
            "only to this session.", severity="warning")

    def _choose_index(self, index: Optional[int]) -> None:
        item = self._item_at(index)
        if item is None:
            self.notify("No matching runtime setting is selected.", severity="warning")
            return
        if self.completion_only:
            self.dismiss(_completion_action(self.kind, item))
            return
        if not self.can_apply:
            self.notify(
                "This picker is read-only; safe session-only selection is "
                "unavailable.", severity="warning")
            return
        self.dismiss(_selection_action(self.kind, item))

    def _select_initial(self) -> None:
        picker = self.query_one("#runtime-setting-options", OptionList)
        index = next((position for position, item in enumerate(self._visible)
                      if item.current), 0) if self._visible else None
        picker.highlighted = index
        self._update_detail(index)

    def _update_detail(self, index: Optional[int]) -> None:
        self.query_one("#runtime-setting-detail", Static).update(
            self._detail(index))

    def _item_at(self, index: Optional[int]) -> Optional[_PickerItem]:
        if index is None or index < 0 or index >= len(self._visible):
            return None
        return self._visible[index]

    def _options(self) -> list[Option]:
        return [Option(
            item.completion_label if self.completion_only else item.label,
            id=f"runtime-setting-{position}")
                for position, item in enumerate(self._visible)]

    def _heading(self) -> str:
        label = "Models" if self.kind == "model" else "Thinking levels"
        visible = len(self._visible)
        count = (f"showing {visible} of {self._matching_count} matching"
                 if visible < self._matching_count else f"{visible} matching")
        access = " · completion" if self.completion_only else (
            " · read-only" if not self.can_apply else "")
        return f"{label} · {count}{access}"

    def _detail(self, index: Optional[int]) -> str:
        item = self._item_at(index)
        if item is None:
            return "No matching runtime settings. Refine or clear the filter."
        value = (f"{item.provider}/{item.model_id}"
                 if self.kind == "model" else item.level)
        if self.completion_only:
            access = "Enter inserts this value; submit the command to apply it."
        else:
            access = ("Enter applies only to the current session."
                      if self.can_apply
                      else "Read-only: selection cannot be applied.")
        return f"Selected · {_display(value, _LABEL_CELLS)}\n{access}"

    def _hint(self) -> str:
        if self.completion_only:
            return ("Type to filter · ↑/↓ wrap · Tab/Enter insert "
                    "into composer "
                    "· Esc/Ctrl+C cancel")
        return ("Type to filter · ↑/↓ wrap · Enter apply to this session "
                "· Esc/Ctrl+C cancel · Ctrl+S unavailable")


def _picker_items(
        kind: str, state: RuntimeSessionState,
        choices: RuntimeModelCatalog | RuntimeThinkingLevels
        ) -> tuple[_PickerItem, ...]:
    expected = RuntimeModelCatalog if kind == "model" else RuntimeThinkingLevels
    if not isinstance(choices, expected):
        raise TypeError(f"{kind} choices have the wrong runtime type")
    if choices.session_id != state.session_id:
        raise ValueError("runtime choices do not match the session state")
    if kind == "model":
        return tuple(_model_item(model, state) for model in choices.models)
    if not all(isinstance(level, str) for level in choices.levels):
        raise TypeError("thinking levels must be strings")
    return tuple(_thinking_item(level, state) for level in choices.levels)


def _model_item(model: RuntimeModelRef, state: RuntimeSessionState) -> _PickerItem:
    if not isinstance(model, RuntimeModelRef):
        raise TypeError("model choices must be RuntimeModelRef values")
    current = bool(state.model is not None
                   and state.model.provider == model.provider
                   and state.model.model_id == model.model_id)
    identity = f"{model.provider}/{model.model_id}"
    suffix = f" — {_display(model.name, 48)}" if _plain(model.name) else ""
    label = _display(f"{'✓' if current else ' '} {identity}{suffix}", _LABEL_CELLS)
    completion_search = _pi_search_text(
        model.model_id, model.provider, identity,
        f"{model.provider} {model.model_id}", model.name)
    search = _search_text(
        completion_search, "current" if current else "")
    return _PickerItem(model.provider, model.model_id, label=label,
                       search=search, completion_label=_display(
                           f"{model.model_id} — {model.provider}", _LABEL_CELLS),
                       completion_search=completion_search, current=current)


def _thinking_item(level: str, state: RuntimeSessionState) -> _PickerItem:
    current = level == state.thinking_level
    description = _THINKING_DESCRIPTIONS.get(level.casefold(), "Provider level")
    label = _display(
        f"{'✓' if current else ' '} {level} — {description}", _LABEL_CELLS)
    search = _search_text(level, description, "current" if current else "")
    return _PickerItem(
        level=level, label=label, search=search,
        completion_label=_display(level, _LABEL_CELLS),
        completion_search=_pi_search_text(level), current=current)


def _filter_items(
        kind: str, items: tuple[_PickerItem, ...], query: str, *,
        pi_compatible: bool = False
        ) -> tuple[tuple[_PickerItem, ...], int]:
    if not _query_key(query):
        matched = items
        if kind == "model" and not pi_compatible:
            matched = tuple(item for item in items if item.current) + tuple(
                item for item in items if not item.current)
        visible = matched[:_MODEL_VISIBLE_LIMIT] if kind == "model" else matched
        return visible, len(matched)
    if pi_compatible:
        return _pi_filter_items(kind, items, query)
    scored = []
    for position, item in enumerate(items):
        score = _fuzzy_score(query, item.search)
        if score is not None:
            scored.append((score, not item.current, position, item))
    scored.sort(key=lambda value: (
        value[0][:3], value[1], value[0][3], value[2]))
    matched = tuple(value[3] for value in scored)
    visible = matched[:_MODEL_VISIBLE_LIMIT] if kind == "model" else matched
    return visible, len(matched)


def _pi_filter_items(
        kind: str, items: tuple[_PickerItem, ...], query: str
        ) -> tuple[tuple[_PickerItem, ...], int]:
    scored = []
    for position, item in enumerate(items):
        score = _pi_fuzzy_score(query, item.completion_search)
        if score is not None:
            scored.append((score, position, item))
    scored.sort(key=lambda value: (value[0], value[1]))
    matched = tuple(value[2] for value in scored)
    visible = matched[:_MODEL_VISIBLE_LIMIT] if kind == "model" else matched
    return visible, len(matched)


def _pi_fuzzy_score(query: str, text: str) -> Optional[float]:
    tokens = tuple(
        token for token in re.split(r"[\s/]+", query.strip()) if token)
    if not tokens:
        return 0.0
    total = 0.0
    for token in tokens:
        score = _pi_match_with_swap(token, text)
        if score is None:
            return None
        total += score
    return total


def _pi_match_with_swap(query: str, text: str) -> Optional[float]:
    primary = _pi_match(query.lower(), text.lower())
    if primary is not None:
        return primary
    alpha_numeric = re.fullmatch(r"([a-z]+)([0-9]+)", query.lower())
    numeric_alpha = re.fullmatch(r"([0-9]+)([a-z]+)", query.lower())
    swapped = (f"{alpha_numeric.group(2)}{alpha_numeric.group(1)}"
               if alpha_numeric else (
                   f"{numeric_alpha.group(2)}{numeric_alpha.group(1)}"
                   if numeric_alpha else ""))
    score = _pi_match(swapped, text.lower()) if swapped else None
    return score + 5 if score is not None else None


def _pi_match(query: str, text: str) -> Optional[float]:
    if not query:
        return 0.0
    if len(query) > len(text):
        return None
    query_index = 0
    score = 0.0
    last_match = -1
    consecutive = 0
    for index, character in enumerate(text):
        if query_index >= len(query) or character != query[query_index]:
            continue
        boundary = index == 0 or _pi_word_boundary(text[index - 1])
        if last_match == index - 1:
            consecutive += 1
            score -= consecutive * 5
        else:
            consecutive = 0
            if last_match >= 0:
                score += (index - last_match - 1) * 2
        if boundary:
            score -= 10
        score += index * 0.1
        last_match = index
        query_index += 1
    if query_index < len(query):
        return None
    return score - 100 if query == text else score


def _pi_word_boundary(character: str) -> bool:
    return character.isspace() or character in "-_./:"


def _fuzzy_score(query: str, candidate: str) -> Optional[tuple[int, int, int, int]]:
    tokens = tuple(
        token for token in _query_key(query).replace("/", " ").split()
        if token)
    if not tokens:
        return 0, 0, 0, len(candidate)
    scores = tuple(_fuzzy_token_score(token, candidate) for token in tokens)
    if any(score is None for score in scores):
        return None
    matched = tuple(score for score in scores if score is not None)
    return (
        sum(score[0] for score in matched),
        sum(score[1] for score in matched),
        sum(score[2] for score in matched),
        len(candidate),
    )


def _fuzzy_token_score(
        needle: str, candidate: str) -> Optional[tuple[int, int, int]]:
    exact = candidate.find(needle)
    if exact >= 0:
        boundary = exact == 0 or not candidate[exact - 1].isalnum()
        return 0 if boundary else 1, exact, 0
    positions = []
    cursor = 0
    for character in needle:
        found = candidate.find(character, cursor)
        if found < 0:
            return None
        positions.append(found)
        cursor = found + 1
    gaps = positions[-1] - positions[0] + 1 - len(positions)
    return 2, gaps, positions[0]


def _selection_action(kind: str, item: _PickerItem) -> RuntimeSettingPickerAction:
    operation = "unchanged" if item.current else "select"
    if kind == "model":
        return RuntimeSettingPickerAction(
            operation, kind, provider=item.provider, model_id=item.model_id)
    return RuntimeSettingPickerAction(operation, kind, level=item.level)


def _completion_action(
        kind: str, item: _PickerItem) -> RuntimeSettingPickerAction:
    if kind == "model":
        return RuntimeSettingPickerAction(
            "complete", kind, provider=item.provider, model_id=item.model_id)
    return RuntimeSettingPickerAction("complete", kind, level=item.level)


def _query_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("initial_query must be a string")
    return _plain(value)[:_QUERY_CHARS]


def _query_key(value: str) -> str:
    return _plain(value).casefold()


def _search_text(*values: str) -> str:
    return " ".join(_plain(value)[:_SEARCH_CHARS].casefold() for value in values)


def _pi_search_text(*values: str) -> str:
    parts = (_plain(value)[:_SEARCH_CHARS].lower() for value in values)
    return " ".join(part for part in parts if part)


def _plain(value: str) -> str:
    text = one_line(clean_text(value))
    return "".join(character for character in text
                   if ord(character) >= 32 and ord(character) != 127)


def _display(value: str, width: int) -> str:
    text = _plain(value)
    if cell_len(text) <= width:
        return text
    return set_cell_size(text, max(0, width - 1)) + "…"
