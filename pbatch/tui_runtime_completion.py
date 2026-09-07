"""Read-only, session-fenced runtime argument completion for the TUI."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional

from textual.app import ScreenStackError
from textual.css.query import NoMatches
from textual.widgets import Input

from .tui_model_picker import (
    RuntimeSettingPickerAction,
    TUIRuntimeSettingPickerScreen,
)
from .tui_runtime import (
    ActiveSessionRequest,
    RuntimeModelCatalog,
    RuntimeModelRef,
    RuntimeSessionState,
    RuntimeThinkingLevels,
)


_COMPLETION_TEXT_MAX_BYTES = 4096
_COMPLETION_CHOICES_MAX = 256
_COMPLETION_TOTAL_MAX_BYTES = 64 * 1024
_COMPLETION_NAME_MAX_BYTES = 1024
_SAFE_ARGUMENT = re.compile(r"[A-Za-z0-9_.\-/~:@]+\Z")


@dataclass(frozen=True)
class RuntimeCompletionContext:
    """A bounded first-line model or thinking argument context."""

    kind: str
    query: str


def parse_runtime_completion_context(
        value: Any) -> Optional[RuntimeCompletionContext]:
    """Recognize an exact command plus its whole argument query."""
    if not isinstance(value, str) or not value.startswith("/"):
        return None
    try:
        if len(value.encode("utf-8")) > _COMPLETION_TEXT_MAX_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    command, separator, query = value[1:].partition(" ")
    if not separator or command not in {"model", "thinking"}:
        return None
    if any(character in "\r\n\x00" for character in query):
        return None
    return RuntimeCompletionContext(command, query)


class TUIRuntimeCompletionMixin:
    """Load one immutable provider snapshot per composer command context."""

    def _init_runtime_completions(self) -> None:
        self._runtime_completion_generation = 0
        self._runtime_completion_kind = ""
        self._runtime_completion_adapter: Any = None
        self._runtime_completion_session_id = ""
        self._runtime_completion_pending = False
        self._runtime_completion_attempted = False
        self._runtime_completion_state: Optional[RuntimeSessionState] = None
        self._runtime_completion_choices: Any = None
        self._runtime_completion_screen: Any = None
        self._runtime_completion_source = ""
        self._runtime_completion_prior_status = ""
        self._runtime_completion_explicit = False

    def _runtime_completion_input(
            self, value: str, trigger: str = "context") -> bool:
        context = parse_runtime_completion_context(value)
        if context is None:
            self._reset_runtime_completion_context()
            return False
        target = self._runtime_completion_target(context.kind)
        if target is None:
            self._reset_runtime_completion_context()
            return False
        adapter, session_id = target
        source_changed = bool(
            self._runtime_completion_screen is not None
            and value != self._runtime_completion_source)
        if (source_changed or not self._same_runtime_completion_context(
                context.kind, adapter, session_id)):
            self._begin_runtime_completion_context(
                context.kind, adapter, session_id)
        self._runtime_completion_source = value
        if trigger == "context":
            return True
        if self._runtime_completion_screen is not None:
            return True
        if self._runtime_completion_choices is not None:
            self._open_runtime_completion(context)
            return True
        if self._runtime_completion_pending:
            return True
        if self._runtime_completion_attempted:
            return True
        self._start_runtime_completion_load(
            context, adapter, session_id, explicit=trigger == "explicit")
        return True

    def _runtime_completion_target(
            self, kind: str) -> Optional[tuple[Any, str]]:
        adapter = getattr(self, "_adapter", None)
        session_id = self._current_session_id()
        capability = "model_catalog" if kind == "model" else "thinking_levels"
        if adapter is None or not session_id or self._runtime_completion_blocked():
            return None
        try:
            runtime, supported = self._runtime_capability(capability)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        return (adapter, session_id) if runtime == "pi-rpc" and supported else None

    def _runtime_completion_blocked(self) -> bool:
        if bool(getattr(self, "_busy", False)):
            return True
        compacting = getattr(self, "_session_compaction_running", None)
        if callable(compacting) and compacting():
            return True
        changing = getattr(self, "_runtime_setting_change_running", None)
        return bool(callable(changing) and changing())

    def _same_runtime_completion_context(
            self, kind: str, adapter: Any, session_id: str) -> bool:
        return bool(
            kind == self._runtime_completion_kind
            and adapter is self._runtime_completion_adapter
            and session_id == self._runtime_completion_session_id)

    def _begin_runtime_completion_context(
            self, kind: str, adapter: Any, session_id: str) -> None:
        self._reset_runtime_completion_context()
        self._runtime_completion_kind = kind
        self._runtime_completion_adapter = adapter
        self._runtime_completion_session_id = session_id

    def _start_runtime_completion_load(
            self, context: RuntimeCompletionContext,
            adapter: Any, session_id: str, *, explicit: bool) -> None:
        self._runtime_completion_generation += 1
        generation = self._runtime_completion_generation
        self._runtime_completion_pending = True
        self._runtime_completion_attempted = True
        self._runtime_completion_explicit = explicit
        self._runtime_completion_prior_status = str(
            getattr(self, "_last_status", "") or "")
        self._last_status = f"Loading runtime {context.kind} completions…"
        self._refresh_status()
        self.run_worker(
            lambda: self._load_runtime_completion(
                generation, adapter, session_id, context.kind),
            name="runtime-completion", group="runtime-completion",
            exit_on_error=False, exclusive=True, thread=True)

    def _load_runtime_completion(
            self, generation: int, adapter: Any,
            session_id: str, kind: str) -> None:
        choices = error = None
        try:
            if not self._runtime_completion_worker_current(
                    generation, adapter, session_id, kind):
                return
            request = ActiveSessionRequest(session_id)
            if kind == "model":
                choices = adapter.get_available_models(request)
            else:
                choices = adapter.get_available_thinking_levels(request)
        except Exception as exc:
            error = exc
        if getattr(self, "is_mounted", True) is False:
            return
        try:
            self.call_from_thread(
                self._finish_runtime_completion, generation, adapter,
                session_id, kind, choices, error)
        except RuntimeError:
            pass

    def _runtime_completion_worker_current(
            self, generation: int, adapter: Any,
            session_id: str, kind: str) -> bool:
        return bool(
            self._runtime_completion_pending
            and generation == self._runtime_completion_generation
            and adapter is self._runtime_completion_adapter
            and adapter is getattr(self, "_adapter", None)
            and session_id == self._runtime_completion_session_id
            and session_id == self._current_session_id()
            and kind == self._runtime_completion_kind
            and not self._runtime_completion_blocked())

    def _finish_runtime_completion(
            self, generation: int, adapter: Any, session_id: str,
            kind: str, choices: Any, error: Optional[Exception]) -> None:
        if not self._runtime_completion_matches(
                generation, adapter, session_id, kind):
            return
        self._runtime_completion_pending = False
        if error is not None:
            self._runtime_completion_unavailable(kind, error)
        elif not _valid_completion_choices(kind, choices, session_id):
            self._runtime_completion_unavailable(kind)
        else:
            bounded = _bounded_completion_choices(kind, choices)
            if _completion_choices_empty(kind, bounded):
                self._runtime_completion_unavailable(kind)
            else:
                self._runtime_completion_choices = bounded
                self._runtime_completion_state = RuntimeSessionState(
                    session_id, "")
                if not self._open_current_runtime_completion():
                    self._last_status = (
                        self._runtime_completion_prior_status or "Ready")
        self._refresh_status()

    def _runtime_completion_unavailable(
            self, kind: str, error: Optional[Exception] = None) -> None:
        if self._runtime_completion_explicit:
            detail = (f": {_completion_error(error)}" if error is not None
                      else ".")
            self.notify(
                f"Runtime {kind} completion is unavailable{detail}",
                severity="warning")
        self._last_status = self._runtime_completion_prior_status or "Ready"

    def _runtime_completion_matches(
            self, generation: int, adapter: Any,
            session_id: str, kind: str) -> bool:
        if not self._runtime_completion_pending:
            return False
        if generation != self._runtime_completion_generation:
            return False
        if not self._same_runtime_completion_context(kind, adapter, session_id):
            return False
        target = self._runtime_completion_target(kind)
        return bool(
            target is not None and target[0] is adapter
            and target[1] == session_id)

    def _open_current_runtime_completion(self) -> bool:
        composer = self.query_one("#composer", Input)
        context = parse_runtime_completion_context(composer.value)
        if (context is None or context.kind != self._runtime_completion_kind
                or not composer.cursor_at_end or self.focused is not composer):
            return False
        self._runtime_completion_source = composer.value
        return self._open_runtime_completion(context)

    def _open_runtime_completion(
            self, context: RuntimeCompletionContext) -> bool:
        state = self._runtime_completion_state
        choices = self._runtime_completion_choices
        if state is None or choices is None:
            return False
        screen = TUIRuntimeSettingPickerScreen(
            context.kind, state, choices, initial_query=context.query,
            can_apply=False, completion_only=True)
        self._runtime_completion_screen = screen
        generation = self._runtime_completion_generation
        adapter = self._runtime_completion_adapter
        session_id = self._runtime_completion_session_id
        source = self._runtime_completion_source
        callback = lambda action: self._finish_runtime_completion_picker(
            generation, adapter, session_id, context.kind,
            screen, source, action)
        try:
            self.push_screen(screen, callback)
        except (RuntimeError, TypeError, ValueError) as exc:
            self._runtime_completion_screen = None
            self.notify(
                f"Unable to open runtime completion: {_completion_error(exc)}",
                severity="warning")
            self._last_status = self._runtime_completion_prior_status or "Ready"
            return False
        self._last_status = f"Choosing runtime {context.kind} completion…"
        self._refresh_status()
        return True

    def _finish_runtime_completion_picker(
            self, generation: int, adapter: Any, session_id: str,
            kind: str, screen: Any, source: str,
            action: Optional[RuntimeSettingPickerAction]) -> None:
        matched = self._runtime_completion_picker_matches(
            generation, adapter, session_id, kind, screen)
        self._runtime_completion_screen = None
        if not matched:
            return
        if action is None:
            self._last_status = self._runtime_completion_prior_status or "Ready"
            self._refresh_status()
            return
        try:
            value = _runtime_completion_value(kind, action)
        except ValueError as exc:
            self.notify(str(exc), severity="warning")
            self._refresh_status()
            return
        composer = self.query_one("#composer", Input)
        if composer.value != source or not composer.cursor_at_end:
            self.notify("That runtime completion expired.", severity="warning")
            self._refresh_status()
            return
        composer.value = value
        composer.cursor_position = len(value)
        composer.focus()
        self._runtime_completion_source = value
        self._last_status = f"Runtime {kind} completion inserted"
        self._refresh_status()

    def _runtime_completion_picker_matches(
            self, generation: int, adapter: Any, session_id: str,
            kind: str, screen: Any) -> bool:
        if (generation != self._runtime_completion_generation
                or screen is not self._runtime_completion_screen
                or not self._same_runtime_completion_context(
                    kind, adapter, session_id)):
            return False
        target = self._runtime_completion_target(kind)
        return bool(
            target is not None and target[0] is adapter
            and target[1] == session_id)

    def _runtime_completion_status_lines(self) -> list[str]:
        if self._runtime_completion_pending:
            return ["complete  loading"]
        if self._runtime_completion_screen is not None:
            return ["complete  choosing"]
        return []

    def _invalidate_runtime_completions(self) -> None:
        self._reset_runtime_completion_context()

    def _reset_runtime_completion_context(self) -> None:
        if not self._runtime_completion_active():
            return
        restore_status = bool(
            self._runtime_completion_pending
            or self._runtime_completion_screen is not None)
        self._runtime_completion_generation += 1
        self._runtime_completion_pending = False
        self._runtime_completion_attempted = False
        self._runtime_completion_kind = ""
        self._runtime_completion_adapter = None
        self._runtime_completion_session_id = ""
        self._runtime_completion_state = None
        self._runtime_completion_choices = None
        self._runtime_completion_source = ""
        self._runtime_completion_explicit = False
        screen, self._runtime_completion_screen = (
            self._runtime_completion_screen, None)
        if restore_status:
            self._last_status = self._runtime_completion_prior_status or "Ready"
        self._runtime_completion_prior_status = ""
        if screen is not None and getattr(screen, "is_mounted", False):
            try:
                screen.dismiss(None)
            except (RuntimeError, ScreenStackError):
                pass
        if getattr(self, "is_mounted", False):
            try:
                self._refresh_status()
            except (NoMatches, RuntimeError):
                pass

    def _runtime_completion_active(self) -> bool:
        return bool(
            self._runtime_completion_kind
            or self._runtime_completion_pending
            or self._runtime_completion_attempted
            or self._runtime_completion_choices is not None
            or self._runtime_completion_screen is not None)


def _valid_completion_choices(kind: str, choices: Any, session_id: str) -> bool:
    expected = RuntimeModelCatalog if kind == "model" else RuntimeThinkingLevels
    return isinstance(choices, expected) and choices.session_id == session_id


def _bounded_completion_choices(kind: str, choices: Any) -> Any:
    if kind == "model":
        return _bounded_model_choices(choices)
    return _bounded_thinking_choices(choices)


def _bounded_model_choices(catalog: RuntimeModelCatalog) -> RuntimeModelCatalog:
    candidates = []
    identities: dict[str, int] = {}
    for model in catalog.models:
        if not isinstance(model, RuntimeModelRef):
            continue
        canonical = f"{model.provider}/{model.model_id}"
        if (not _safe_completion_argument(model.provider)
                or not _safe_completion_argument(model.model_id)
                or not _safe_completion_argument(canonical)):
            continue
        key = canonical.casefold()
        identities[key] = identities.get(key, 0) + 1
        candidates.append((key, model, canonical))
    accepted = []
    total = 0
    for key, model, canonical in candidates:
        if identities[key] != 1:
            continue
        name = _bounded_display_name(model.name)
        size = len(canonical.encode("utf-8")) + len(name.encode("utf-8"))
        if total + size > _COMPLETION_TOTAL_MAX_BYTES:
            break
        accepted.append(RuntimeModelRef(model.provider, model.model_id, name))
        total += size
        if len(accepted) >= _COMPLETION_CHOICES_MAX:
            break
    return RuntimeModelCatalog(catalog.session_id, tuple(accepted))


def _bounded_thinking_choices(
        choices: RuntimeThinkingLevels) -> RuntimeThinkingLevels:
    counts: dict[str, int] = {}
    for level in choices.levels:
        if isinstance(level, str) and _safe_completion_argument(level):
            key = level.casefold()
            counts[key] = counts.get(key, 0) + 1
    accepted = []
    total = 0
    for level in choices.levels:
        if (not isinstance(level, str) or not _safe_completion_argument(level)
                or counts.get(level.casefold()) != 1):
            continue
        size = len(level.encode("utf-8"))
        if total + size > _COMPLETION_TOTAL_MAX_BYTES:
            break
        accepted.append(level)
        total += size
        if len(accepted) >= _COMPLETION_CHOICES_MAX:
            break
    return RuntimeThinkingLevels(choices.session_id, tuple(accepted))


def _safe_completion_argument(value: str) -> bool:
    if not isinstance(value, str) or not _SAFE_ARGUMENT.fullmatch(value):
        return False
    try:
        return len(value.encode("utf-8")) <= _COMPLETION_TEXT_MAX_BYTES - 12
    except UnicodeEncodeError:
        return False


def _bounded_display_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    clean = " ".join(value.split())
    encoded = clean.encode("utf-8", "ignore")
    if len(encoded) <= _COMPLETION_NAME_MAX_BYTES:
        return clean
    return encoded[:_COMPLETION_NAME_MAX_BYTES].decode("utf-8", "ignore")


def _completion_choices_empty(kind: str, choices: Any) -> bool:
    return not (choices.models if kind == "model" else choices.levels)


def _runtime_completion_value(
        kind: str, action: RuntimeSettingPickerAction) -> str:
    if action.operation != "complete" or action.kind != kind:
        raise ValueError("Runtime completion returned invalid data.")
    if (kind == "model" and (
            not _safe_completion_argument(action.provider)
            or not _safe_completion_argument(action.model_id))):
        raise ValueError("Runtime completion contains an unsafe value.")
    raw = (f"{action.provider}/{action.model_id}"
           if kind == "model" else action.level)
    if not _safe_completion_argument(raw):
        raise ValueError("Runtime completion contains an unsafe value.")
    value = f"/{kind} {raw}"
    try:
        if len(value.encode("utf-8")) > _COMPLETION_TEXT_MAX_BYTES:
            raise ValueError("Runtime completion exceeds the composer boundary.")
    except UnicodeEncodeError as exc:
        raise ValueError("Runtime completion is not valid UTF-8 text.") from exc
    return value


def _completion_error(error: Exception) -> str:
    text = " ".join(str(error).split())
    return (text or error.__class__.__name__)[:512]
