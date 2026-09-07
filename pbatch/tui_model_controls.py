"""Typed active-session model and thinking controls for the product TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .tui_model_picker import (
    RuntimeSettingPickerAction,
    TUIRuntimeSettingPickerScreen,
)
from .tui_runtime_completion import TUIRuntimeCompletionMixin
from .tui_runtime import (
    ActiveSessionRequest,
    RuntimeModelCatalog,
    RuntimeSessionState,
    RuntimeThinkingLevels,
    SetModelRequest,
    SetThinkingLevelRequest,
)
from .tui_view_support import clean_text, one_line


_RENDER_MAX_BYTES = 32 * 1024
_RENDER_MAX_MODELS = 48


@dataclass(frozen=True)
class _ControlResult:
    state: Any
    choices: Any = None
    warning: str = ""


def runtime_setting_change_active(owner: Any) -> bool:
    """Let sibling mixins observe the logical setting-mutation lock."""
    check = getattr(owner, "_runtime_setting_change_running", None)
    return bool(check()) if callable(check) else False


def render_model_catalog(
        catalog: RuntimeModelCatalog, state: RuntimeSessionState,
        *, width: int = 80) -> str:
    """Render a bounded, redacted model catalog and the effective selection."""
    width = max(24, min(160, int(width)))
    current = _model_identity(state)
    current_label = _inline(current, max(1, width - len("Current: ``")))
    lines = ["## Available models", "", f"Current: `{_code(current_label)}`", ""]
    for model in catalog.models[:_RENDER_MAX_MODELS]:
        identity = _inline(
            f"{model.provider}/{model.model_id}", max(1, width - 5))
        suffix = f" — `{_code(model.name)}`" if model.name else ""
        marker = " (current)" if identity == current else ""
        lines.append(_fit_line(
            f"- `{_code(identity)}`{suffix}{marker}", width))
    omitted = len(catalog.models) - _RENDER_MAX_MODELS
    if omitted > 0:
        lines.append(f"- … {omitted} more models omitted")
    lines.extend(["", _fit_line(
        "Select with `/model <provider>/<model-id>`.", width)])
    return _bounded_render(lines)


def render_thinking_levels(
        choices: RuntimeThinkingLevels, state: RuntimeSessionState) -> str:
    """Render provider-declared levels and the effective current value."""
    levels = ", ".join(
        f"`{_code(level)}`" for level in choices.levels) or "none"
    current = state.thinking_level or "unknown"
    return _bounded_render([
        "## Thinking level", "", f"Current: `{_code(current)}`",
        "", f"Available: {levels}", "",
        "Select with `/thinking <level>`.",
    ])


class TUIModelControlMixin(TUIRuntimeCompletionMixin):
    """Query and mutate model controls without restarting the Pi session."""

    def _init_model_controls(self) -> None:
        self._init_runtime_completions()
        self._runtime_setting_generation = 0
        self._runtime_setting_pending = False
        self._runtime_setting_adapter: Any = None
        self._runtime_setting_session_id = ""
        self._runtime_setting_operation = ""
        self._runtime_control_state: Optional[RuntimeSessionState] = None
        self._runtime_picker_serial = 0
        self._runtime_picker_open = False
        self._runtime_picker_generation = 0
        self._runtime_picker_adapter: Any = None
        self._runtime_picker_session_id = ""
        self._runtime_picker_kind = ""
        self._runtime_picker_token = 0
        self._runtime_picker_screen: Any = None

    def _slash_model(self, args: tuple[str, ...]) -> None:
        if not self._is_pi_control_runtime():
            if len(args) > 1:
                self._usage("model", "[value]")
                return
            self._show_or_set("model", args)
            return
        query = " ".join(args).strip()
        if args and not query:
            self._usage("model", "[query]")
            return
        self._start_runtime_setting("model_show", query or None)

    def _slash_thinking(self, args: tuple[str, ...]) -> None:
        if len(args) > 1 or (args and not args[0].strip()):
            self._usage("thinking", "[level]")
            return
        if not self._is_pi_control_runtime():
            self._append_entry(
                "error", "The active runtime does not support `/thinking`.")
            return
        if args and not self._session_setting_capability("thinking_select"):
            self._unsafe_setting_unavailable("thinking")
            return
        self._start_runtime_setting(
            "thinking_set" if args else "thinking_show",
            args[0] if args else None)

    def _is_pi_control_runtime(self) -> bool:
        try:
            return self._runtime_capability("session_state")[0] == "pi-rpc"
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def _control_capability(self, name: str) -> bool:
        try:
            return bool(self._runtime_capability(name)[1])
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def _session_setting_capability(
            self, name: str, adapter: Any = None) -> bool:
        target = adapter if adapter is not None else getattr(self, "_adapter", None)
        return _safe_session_setting_capability(target, name)

    def _unsafe_setting_unavailable(self, label: str) -> None:
        self._append_entry(
            "error", f"The active Pi runtime is read-only for {label} selection. "
            "Safe apply-to-session requires Pi 0.84.3 or newer and an exact "
            "session settings scope.")

    def _start_runtime_setting(self, operation: str, requested: Any) -> None:
        if self._runtime_setting_blocked():
            return
        mutation = _mutation_capability(operation)
        if mutation and not self._session_setting_capability(mutation):
            self._unsafe_setting_unavailable(
                "model" if operation.startswith("model") else "thinking")
            return
        required = _required_capabilities(operation)
        adapter, session_id = self._session_control_target(required)
        if adapter is None:
            return
        self._invalidate_runtime_completions()
        generation = self._runtime_setting_generation + 1
        self._runtime_setting_generation = generation
        self._runtime_setting_pending = True
        self._runtime_setting_adapter = adapter
        self._runtime_setting_session_id = session_id
        self._runtime_setting_operation = operation
        self._session_control_generation += 1
        self._session_control_pending = False
        self._last_status = _operation_status(operation)
        self._refresh_status()
        self.run_worker(
            lambda: self._run_runtime_setting(
                generation, adapter, session_id, operation, requested),
            name="runtime-setting", group="runtime-setting",
            exit_on_error=False, exclusive=True, thread=True)

    def _runtime_setting_blocked(self) -> bool:
        if self._busy or self._session_compaction_running():
            self.notify("Wait for the active operation before changing settings.",
                        severity="warning")
            return True
        if self._runtime_setting_change_running():
            self.notify("A runtime setting operation is already running.",
                        severity="warning")
            return True
        return False

    def _run_runtime_setting(
            self, generation: int, adapter: Any, session_id: str,
            operation: str, requested: Any) -> None:
        result = error = None
        try:
            result = _execute_control(
                adapter, session_id, operation, requested)
        except Exception as exc:
            error = exc
        if getattr(self, "is_mounted", True) is False:
            return
        try:
            self.call_from_thread(
                self._finish_runtime_setting, generation, adapter, session_id,
                operation, requested, result, error)
        except RuntimeError:
            pass

    def _finish_runtime_setting(
            self, generation: int, adapter: Any, session_id: str,
            operation: str, requested: Any, result: Any,
            error: Optional[Exception]) -> None:
        if not self._runtime_setting_matches(
                generation, adapter, session_id, operation):
            return
        self._clear_runtime_setting()
        if error is not None:
            self._render_runtime_setting_error(operation, error)
        else:
            try:
                self._render_runtime_setting_result(
                    generation, adapter, session_id,
                    operation, requested, result)
            except (TypeError, ValueError) as exc:
                self._render_runtime_setting_error(operation, exc)
        self._refresh_status()

    def _render_runtime_setting_error(
            self, operation: str, error: Exception) -> None:
        action = "update" if operation.endswith("_set") else "query"
        detail = _error_text(error)
        self._append_entry(
            "error", f"Runtime setting {action} could not be confirmed: "
            f"{detail}. Run `/session` to reconcile the effective state.")
        self._last_status = f"Runtime setting {action} unconfirmed"

    def _render_runtime_setting_result(
            self, generation: int, adapter: Any, session_id: str,
            operation: str, requested: Any, result: Any) -> None:
        state, choices, warning = _validated_result(
            operation, result, session_id)
        if operation.endswith("_set"):
            self._invalidate_runtime_completions()
        self._record_runtime_control_state(state)
        if operation.endswith("_show"):
            self._handle_runtime_setting_query(
                generation, adapter, session_id, operation,
                requested, state, choices)
            return
        text = _render_setting_readback(
            operation, requested, state, choices)
        self._append_entry("system", text)
        if warning:
            self._append_entry(
                "system", "Model updated, but the new thinking choices "
                f"could not be refreshed: {warning}.")
        self._last_status = "Runtime setting updated"

    def _handle_runtime_setting_query(
            self, generation: int, adapter: Any, session_id: str,
            operation: str, requested: Any, state: RuntimeSessionState,
            choices: Any) -> None:
        if operation == "model_show" and requested:
            selected = _find_exact_model(str(requested), choices)
            if selected is not None and _model_matches(state, selected):
                self.notify("That model is already active for this session.")
                self._last_status = "Runtime model unchanged"
                return
            if selected is not None and self._session_setting_capability(
                    "model_select", adapter):
                self._start_runtime_setting(
                    "model_set", (selected.provider, selected.model_id))
                return
        kind = "model" if operation == "model_show" else "thinking"
        self._open_runtime_setting_picker(
            generation, adapter, session_id, kind, state, choices,
            initial_query=str(requested or ""))

    def _open_runtime_setting_picker(
            self, generation: int, adapter: Any, session_id: str,
            kind: str, state: RuntimeSessionState, choices: Any,
            *, initial_query: str = "") -> None:
        if not self._picker_opening_matches(
                generation, adapter, session_id):
            return
        capability = "model_select" if kind == "model" else "thinking_select"
        screen = TUIRuntimeSettingPickerScreen(
            kind, state, choices, initial_query=initial_query,
            can_apply=self._session_setting_capability(capability, adapter))
        token = self._register_runtime_picker(
            generation, adapter, session_id, kind, screen)
        callback = lambda action: self._handle_runtime_picker_action(
            token, generation, adapter, session_id, kind, action)
        try:
            self.push_screen(screen, callback)
        except (RuntimeError, TypeError, ValueError) as exc:
            self._clear_runtime_picker()
            self._append_entry(
                "error", f"Unable to open runtime setting picker: "
                f"{_error_text(exc)}")
            self._last_status = "Runtime setting picker failed"

    def _picker_opening_matches(
            self, generation: int, adapter: Any, session_id: str) -> bool:
        return bool(
            generation == self._runtime_setting_generation
            and adapter is getattr(self, "_adapter", None)
            and session_id == self._current_session_id())

    def _register_runtime_picker(
            self, generation: int, adapter: Any, session_id: str,
            kind: str, screen: Any) -> int:
        self._runtime_picker_serial += 1
        token = self._runtime_picker_serial
        self._runtime_picker_open = True
        self._runtime_picker_generation = generation
        self._runtime_picker_adapter = adapter
        self._runtime_picker_session_id = session_id
        self._runtime_picker_kind = kind
        self._runtime_picker_token = token
        self._runtime_picker_screen = screen
        self._last_status = f"Choosing runtime {kind}…"
        self._refresh_status()
        return token

    def _handle_runtime_picker_action(
            self, token: int, generation: int, adapter: Any,
            session_id: str, kind: str,
            action: Optional[RuntimeSettingPickerAction]) -> None:
        matched = self._runtime_picker_matches(
            token, generation, adapter, session_id, kind)
        self._clear_runtime_picker()
        if not matched:
            if action is not None:
                self.notify(
                    "Those runtime choices expired; open the picker again.",
                    severity="warning")
            self._refresh_status()
            return
        if action is None:
            self._last_status = "Runtime setting selection cancelled"
            self._refresh_status()
            return
        if action.operation == "unchanged":
            self._last_status = "Runtime setting unchanged"
            self._refresh_status()
            return
        if action.operation != "select" or action.kind != kind:
            self._append_entry("error", "Runtime setting picker returned invalid data.")
            self._refresh_status()
            return
        capability = "model_select" if kind == "model" else "thinking_select"
        if not self._session_setting_capability(capability, adapter):
            self._unsafe_setting_unavailable(kind)
            self._refresh_status()
            return
        try:
            operation, requested = _picker_selection(action)
        except ValueError as exc:
            self._append_entry("error", _error_text(exc))
            self._refresh_status()
            return
        self._start_runtime_setting(operation, requested)

    def _runtime_picker_matches(
            self, token: int, generation: int, adapter: Any,
            session_id: str, kind: str) -> bool:
        return bool(
            self._runtime_picker_open
            and token == self._runtime_picker_token
            and generation == self._runtime_picker_generation
            and generation == self._runtime_setting_generation
            and adapter is self._runtime_picker_adapter
            and adapter is getattr(self, "_adapter", None)
            and session_id == self._runtime_picker_session_id
            and session_id == self._current_session_id()
            and kind == self._runtime_picker_kind)

    def _clear_runtime_picker(self) -> None:
        self._runtime_picker_open = False
        self._runtime_picker_generation = 0
        self._runtime_picker_adapter = None
        self._runtime_picker_session_id = ""
        self._runtime_picker_kind = ""
        self._runtime_picker_token = 0
        self._runtime_picker_screen = None

    def _dismiss_runtime_picker(self) -> None:
        screen = self._runtime_picker_screen
        self._clear_runtime_picker()
        if screen is None or not getattr(screen, "is_mounted", False):
            return
        try:
            screen.dismiss(None)
        except RuntimeError:
            pass

    def _handle_runtime_setting_cancel(self) -> bool:
        if getattr(self, "_runtime_picker_open", False):
            self._dismiss_runtime_picker()
            self._last_status = "Runtime setting selection cancelled"
            self._refresh_status()
            return True
        if self._runtime_setting_change_running():
            self.notify("Runtime setting updates cannot be safely cancelled.")
            return True
        return False

    def _runtime_setting_matches(
            self, generation: int, adapter: Any, session_id: str,
            operation: str) -> bool:
        return bool(
            self._runtime_setting_pending
            and generation == self._runtime_setting_generation
            and adapter is getattr(self, "_adapter", None)
            and adapter is self._runtime_setting_adapter
            and session_id == self._current_session_id()
            and session_id == self._runtime_setting_session_id
            and operation == self._runtime_setting_operation)

    def _runtime_setting_change_running(self) -> bool:
        setting = bool(
            getattr(self, "_runtime_setting_pending", False)
            and self._runtime_setting_adapter is getattr(self, "_adapter", None)
            and self._runtime_setting_session_id == self._current_session_id())
        choosing = bool(
            getattr(self, "_runtime_picker_open", False)
            and self._runtime_picker_adapter is getattr(self, "_adapter", None)
            and self._runtime_picker_session_id == self._current_session_id())
        return setting or choosing

    def _session_control_busy(self) -> bool:
        return self._session_compaction_running() or self._runtime_setting_change_running()

    def _record_runtime_control_state(self, state: Any) -> None:
        if (isinstance(state, RuntimeSessionState)
                and state.session_id == self._current_session_id()):
            self._runtime_control_state = state

    def _invalidate_model_controls(self) -> None:
        self._runtime_setting_generation += 1
        self._clear_runtime_setting()
        self._dismiss_runtime_picker()
        self._invalidate_runtime_completions()
        self._runtime_control_state = None

    def _clear_runtime_setting(self) -> None:
        self._runtime_setting_pending = False
        self._runtime_setting_adapter = None
        self._runtime_setting_session_id = ""
        self._runtime_setting_operation = ""

    def _model_control_status_lines(self) -> list[str]:
        lines = self._runtime_completion_status_lines()
        if getattr(self, "_runtime_picker_open", False):
            lines.append("setting   choosing")
        elif self._runtime_setting_change_running():
            lines.append("setting   running")
        return lines

    def _runtime_control_option_values(
            self) -> Optional[tuple[str, str, str]]:
        state = getattr(self, "_runtime_control_state", None)
        if (not isinstance(state, RuntimeSessionState)
                or state.session_id != self._current_session_id()):
            return None
        model = state.model
        return (
            model.model_id if model is not None else "",
            model.provider if model is not None else "",
            state.thinking_level,
        )


def _execute_control(
        adapter: Any, session_id: str, operation: str, requested: Any) -> Any:
    request = ActiveSessionRequest(session_id)
    if operation == "model_show":
        return _ControlResult(
            adapter.get_session_state(request),
            adapter.get_available_models(request))
    if operation == "thinking_show":
        return _ControlResult(
            adapter.get_session_state(request),
            adapter.get_available_thinking_levels(request))
    if operation == "model_set":
        _require_safe_session_setting(adapter, "model_select")
        catalog = adapter.get_available_models(request)
        provider, model_id = requested
        if not _catalog_contains(catalog, provider, model_id):
            raise ValueError("the requested provider/model is not available")
        state = adapter.set_model(SetModelRequest(provider, model_id, session_id))
        try:
            choices = adapter.get_available_thinking_levels(request)
            return _ControlResult(state, choices)
        except Exception as exc:
            return _ControlResult(state, warning=_error_text(exc))
    _require_safe_session_setting(adapter, "thinking_select")
    levels = adapter.get_available_thinking_levels(request)
    selected = _find_thinking_level(str(requested), levels)
    if selected is None:
        available = ", ".join(levels.levels) if isinstance(
            levels, RuntimeThinkingLevels) else "none"
        raise ValueError(
            f'unknown thinking level "{requested}"; available levels: '
            f"{available or 'none'}")
    state = adapter.set_thinking_level(
        SetThinkingLevelRequest(selected, session_id))
    return _ControlResult(state, selected)


def _required_capabilities(operation: str) -> tuple[str, ...]:
    return {
        "model_show": ("session_state", "model_catalog"),
        "model_set": ("model_catalog", "model_select"),
        "thinking_show": ("session_state", "thinking_levels"),
        "thinking_set": ("thinking_levels", "thinking_select"),
    }[operation]


def _mutation_capability(operation: str) -> str:
    return {
        "model_set": "model_select",
        "thinking_set": "thinking_select",
    }.get(operation, "")


def _operation_status(operation: str) -> str:
    label = "model" if operation.startswith("model") else "thinking level"
    action = "Updating" if operation.endswith("_set") else "Loading"
    return f"{action} runtime {label}…"


def _validated_result(
        operation: str, value: Any,
        session_id: str) -> tuple[RuntimeSessionState, Any, str]:
    if not isinstance(value, _ControlResult):
        raise ValueError("runtime setting query returned invalid data")
    state, choices, warning = value.state, value.choices, value.warning
    if not isinstance(state, RuntimeSessionState) or state.session_id != session_id:
        raise ValueError("runtime setting response does not match the active session")
    if operation == "model_show" and not isinstance(choices, RuntimeModelCatalog):
        raise ValueError("runtime model catalog is invalid")
    if operation == "thinking_show" and not isinstance(
            choices, RuntimeThinkingLevels):
        raise ValueError("runtime thinking choices are invalid")
    if (operation == "model_set" and choices is not None
            and not isinstance(choices, RuntimeThinkingLevels)):
        raise ValueError("runtime thinking choices are invalid")
    if operation == "thinking_set" and not isinstance(choices, str):
        raise ValueError("runtime thinking selection is invalid")
    if (choices is not None and not isinstance(choices, str)
            and choices.session_id != session_id):
        raise ValueError("runtime setting choices do not match the active session")
    return state, choices, warning


def _catalog_contains(
        catalog: Any, provider: str, model_id: str) -> bool:
    if not isinstance(catalog, RuntimeModelCatalog):
        raise ValueError("runtime model catalog is invalid")
    return any(
        item.provider == provider and item.model_id == model_id
        for item in catalog.models)


def _render_setting_readback(
        operation: str, requested: Any, state: RuntimeSessionState,
        choices: Any = None) -> str:
    if operation == "model_set":
        provider, model_id = requested
        effective = _model_identity(state)
        if effective != f"{provider}/{model_id}":
            raise ValueError("runtime model readback did not match the requested model")
        levels = ", ".join(choices.levels) if isinstance(
            choices, RuntimeThinkingLevels) else "unknown"
        return _bounded_render([
            "## Model updated", "", f"Effective: `{_code(effective)}`",
            f"Thinking: `{_code(state.thinking_level or 'unknown')}`",
            f"Available thinking: `{_code(levels or 'none')}`",
        ])
    selected = choices if isinstance(choices, str) else requested
    return _bounded_render([
        "## Thinking level updated", "", f"Requested: `{_code(selected)}`",
        f"Effective: `{_code(state.thinking_level or 'unknown')}`",
    ])


def _find_exact_model(value: str, catalog: Any) -> Any:
    if not isinstance(catalog, RuntimeModelCatalog):
        raise ValueError("runtime model catalog is invalid")
    query = value.strip().casefold()
    if not query:
        return None
    canonical = tuple(model for model in catalog.models
                      if f"{model.provider}/{model.model_id}".casefold() == query)
    if len(canonical) == 1:
        return canonical[0]
    if canonical:
        return None
    by_id = tuple(model for model in catalog.models
                  if model.model_id.casefold() == query)
    return by_id[0] if len(by_id) == 1 else None


def _find_thinking_level(value: str, levels: Any) -> Optional[str]:
    if not isinstance(levels, RuntimeThinkingLevels):
        return None
    normalized = value.strip().casefold()
    matches = tuple(level for level in levels.levels
                    if level.casefold() == normalized)
    return matches[0] if len(matches) == 1 else None


def _model_matches(state: RuntimeSessionState, model: Any) -> bool:
    return bool(
        state.model is not None
        and state.model.provider == getattr(model, "provider", None)
        and state.model.model_id == getattr(model, "model_id", None))


def _picker_selection(
        action: RuntimeSettingPickerAction) -> tuple[str, Any]:
    if action.kind == "model" and action.provider and action.model_id:
        return "model_set", (action.provider, action.model_id)
    if action.kind == "thinking" and action.level:
        return "thinking_set", action.level
    raise ValueError("Runtime setting picker returned an empty selection.")


def _safe_session_setting_capability(adapter: Any, name: str) -> bool:
    if adapter is None:
        return False
    try:
        info = adapter.runtime_info()
        capabilities = getattr(info, "capabilities", None)
        supports = getattr(capabilities, "supports", None)
        return bool(
            getattr(info, "runtime", "") == "pi-rpc"
            and getattr(info, "settings_scope", "") == "session"
            and callable(supports) and supports(name))
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _require_safe_session_setting(adapter: Any, name: str) -> None:
    if not _safe_session_setting_capability(adapter, name):
        raise RuntimeError(
            "safe session-only runtime setting capability is unavailable")


def _model_identity(state: RuntimeSessionState) -> str:
    if state.model is None:
        return "unknown"
    return f"{state.model.provider}/{state.model.model_id}"


def _inline(value: Any, width: int) -> str:
    maximum = max(16, min(160, int(width)))
    cleaned = clean_text(value)
    text = one_line("".join(
        char for char in cleaned if ord(char) >= 32 and ord(char) != 127))
    return text if len(text) <= maximum else text[:maximum - 1] + "…"


def _code(value: Any) -> str:
    return _inline(value, 160).replace("`", "'")


def _bounded_render(lines: list[str]) -> str:
    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    if len(encoded) <= _RENDER_MAX_BYTES:
        return text
    marker = "\n\n… output truncated"
    marker_bytes = marker.encode("utf-8")
    prefix = encoded[:_RENDER_MAX_BYTES - len(marker_bytes)].decode(
        "utf-8", "ignore")
    return prefix + marker


def _fit_line(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[:max(0, width - 1)] + "…"


def _error_text(error: Exception) -> str:
    text = _inline(error, 512)
    return text or error.__class__.__name__
