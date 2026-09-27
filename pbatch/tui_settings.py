"""Runtime option controls shared by the interactive TUI."""
from __future__ import annotations

import os
from typing import Any, Optional

from .tui_model_controls import (
    TUIModelControlMixin,
    runtime_setting_change_active,
)
from .tui_session_controls import session_compaction_active
from .tui_view_support import (
    is_codex_binary as _is_codex_binary,
    is_pi_binary as _is_pi_binary,
    one_line as _one_line,
    short_path as _short_path,
)


class TUISettingsMixin(TUIModelControlMixin):
    """Render and update runtime options without owning TUI lifecycle state."""

    def _option_status_lines(self, snapshot: Optional[dict] = None) -> list[str]:
        cwd = self.options.cwd or os.getcwd()
        effective = self._runtime_control_option_values()
        model = effective[0] if effective else self._option_value('model')
        provider = effective[1] if effective else self._option_value('provider')
        lines = [
            f"workspace {_short_path(cwd, 20)}",
            f"agent     {self._option_value('agent') or 'default'}",
            f"model     {model or 'default'}",
            f"provider  {provider or 'default'}",
            f"runtime   {self.options.runtime or 'auto'}",
            f"memory    {self._option_value('memory_mode') or 'auto'}",
        ]
        lines.extend(self._runtime_policy_status_lines())
        lines.extend(self._runtime_checkpoint_status_lines())
        if effective:
            lines.append(f"thinking  {effective[2] or 'unknown'}")
        workspace = (snapshot or {}).get("workspace", {})
        if isinstance(workspace, dict) and workspace.get("available"):
            lines.extend([
                f"branch    {_one_line(str(workspace.get('branch', '')))[:20]}",
                f"changes   {workspace.get('changed', 0)}",
            ])
        return lines

    def _runtime_policy_status_lines(self) -> list[str]:
        """Render only policy values accepted by the typed projection."""
        view = getattr(self, "_runtime_view", None)
        policy = getattr(view, "policy", None)
        if policy is None:
            return []
        labels = (("approval", "approval"), ("sandbox", "sandbox"),
                  ("network", "network"))
        return [f"{label:<9} {_one_line(str(getattr(policy, key, 'unknown')))[:20]}"
                for key, label in labels]

    def _runtime_checkpoint_status_lines(self) -> list[str]:
        if not getattr(self, "_projection_driven", False):
            return []
        status = str(getattr(self._event_controller, "checkpoint_status", "") or "")
        return [f"checkpoint {status[:20]}"] if status else []

    def _option_value(self, name: str) -> Any:
        if name == "runtime":
            return self.options.runtime
        if self._backend is not None and hasattr(self._backend, name):
            return getattr(self._backend, name)
        backend_options = getattr(self._backend, "options", None)
        if isinstance(backend_options, dict) and name in backend_options:
            return backend_options[name]
        return getattr(self.options, name)

    def _pi_runtime_agent(self, agent: str) -> tuple[str, bool]:
        canonical = _is_pi_binary(agent)
        if not self.options.agent and canonical:
            from .pi_version import select_pi_executable_token
            agent = select_pi_executable_token(fallback=agent)
        return agent, canonical

    def _set_option(self, name: str, value: str) -> None:
        if runtime_setting_change_active(self):
            self.notify("Wait for the runtime setting operation to finish.",
                        severity="warning")
            return
        if session_compaction_active(self):
            self.notify("Wait for session compaction before changing runtime settings.",
                        severity="warning")
            return
        if self._option_conflicts_with_runtime(name, value):
            self._append_entry(
                "error", "Provider override is unavailable for the selected Codex runtime. "
                "Clear it with `/provider \"\"` before switching.")
            return
        was_blocked = bool(self._runtime_blocked_reason)
        rebind = self._option_rebinds_runtime(name, was_blocked)
        if rebind and not self._prepare_runtime_action(session_switch=True):
            return
        setattr(self.options, name, value)
        if not self._set_backend_option(name, value):
            return
        if rebind:
            try:
                self._rebind_runtime()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._runtime_blocked_reason = (
                    "Runtime setting change did not complete; resolve the journal "
                    "or runtime error, then use `/new` before running a turn.")
                self._append_entry("error", f"Unable to apply {name}: {exc}. "
                                   f"{self._runtime_blocked_reason}")
                return
        self._last_status = f"{name} updated; applies to the next turn"
        self._refresh_status()
        self._append_entry("system", f"`{name}` set to `{value or 'default'}`.")

    def _option_conflicts_with_runtime(self, name: str, value: str) -> bool:
        target_agent = value if name == "agent" else self._option_value("agent")
        target_runtime = value if name == "runtime" else self.options.runtime
        provider = value if name == "provider" else self._option_value("provider")
        codex_runtime = target_runtime == "app-server" or (
            target_runtime in {"auto", "structured"}
            and _is_codex_binary(target_agent)
        )
        return bool(codex_runtime and str(provider or "").strip())

    def _set_backend_option(self, name: str, value: str) -> bool:
        if self._backend is None or name == "runtime":
            return True
        try:
            setattr(self._backend, name, value)
            backend_options = getattr(self._backend, "options", None)
            if isinstance(backend_options, dict):
                backend_options[name] = value
        except (AttributeError, TypeError) as exc:
            self._append_entry("error", f"Unable to set {name}: {exc}")
            return False
        return True

    def _option_rebinds_runtime(self, name: str, was_blocked: bool) -> bool:
        if name in {"agent", "runtime"} or (name == "provider" and was_blocked):
            return True
        runtime = str(self.options.runtime or "auto")
        agent = self._option_value("agent")
        pi_rpc = runtime == "pi-rpc" or (
            runtime == "auto" and getattr(self, "_managed_backend", False)
            and _is_pi_binary(agent))
        return pi_rpc and name in {"model", "provider"}

    def _rebind_runtime(self) -> None:
        """Realign the idle UI adapter and start a runtime-local session."""
        invalidate = getattr(self, "_invalidate_session_controls", None)
        if callable(invalidate):
            invalidate()
        adapter, self._adapter = self._adapter, None
        if adapter is not None:
            adapter.close()
        setter = getattr(self._backend, "set_runtime_event_sink", None)
        if callable(setter):
            setter(None)
        self._adapter_session_id = ""
        self._adapter_workspace_id = ""
        self._structured_backend = False
        self._strict_adapter = False
        self._disable_runtime_projection()
        new_session = getattr(self._backend, "new_session", None)
        if callable(new_session):
            new_session(self.options.session_name)
        elif hasattr(self._backend, "provider_thread_id"):
            self._backend.provider_thread_id = ""
        self._attach_adapter()
        self._prepare_runtime_projection()
        self._reset_follow_ups_for_session_switch()

    def _slash_provider(self, args: tuple[str, ...]) -> None:
        self._show_or_set("provider", args)

    def _slash_agent(self, args: tuple[str, ...]) -> None:
        self._show_or_set("agent", args)

    def _slash_memory(self, args: tuple[str, ...]) -> None:
        if not args:
            value = self._option_value("memory_mode")
            self._append_entry("system", f"`memory_mode`: `{value}`")
            return
        if len(args) != 1 or args[0] not in {"auto", "on", "off"}:
            self._usage("memory", "auto|on|off")
            return
        self._set_option("memory_mode", args[0])

    def _slash_runtime(self, args: tuple[str, ...]) -> None:
        choices = {"auto", "pi-rpc", "app-server", "structured", "legacy"}
        if not args:
            self._append_entry("system", f"`runtime`: `{self.options.runtime or 'auto'}`")
            return
        if len(args) != 1 or args[0] not in choices:
            self._usage("runtime", "auto|pi-rpc|app-server|structured|legacy")
            return
        self._set_option("runtime", args[0])

    def _show_or_set(self, name: str, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._usage(name, "[value]")
        elif not args:
            value = self._option_value(name) or "default"
            self._append_entry("system", f"`{name}`: `{value}`")
        else:
            self._set_option(name, args[0])
