"""Product terminal UI for interactive pi-batch sessions."""
from __future__ import annotations

import os
import sys
import threading
import uuid
from typing import Any, Callable, Iterable, Optional

from textual import events
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Markdown, Static

from . import config
from .tui_adapter_pi_rpc import PiRpcAdapter
from .tui_composer import (
    SlashComposer,
    command_spec,
    palette_callback,
    palette_specs,
)
from .tui_projection_ui import TUIProjectionMixin
from .tui_shell import (
    WELCOME as _WELCOME,
    SlashInvocation,
    TuiOptions,
    parse_slash_command,
)
from .tui_timeline import TimelineLog
from .tui_transcript import (
    OMISSION_MARKDOWN,
    TranscriptEntry,
    trim_transcript,
)
from .tui_commands import (
    TUIRuntimeLifecycleMixin, TUIActorControlMixin, TUIProductMixin, TUIReviewMixin, TUIPipelineMixin,
    TUIWorkspaceMixin)
from .tui_agent_runs import TUIAgentsMixin
from .tui_inspector import TUIInspectorMixin
from .tui_followups import TUIFollowUpMixin
from .tui_approvals import TUIApprovalMixin
from .tui_sessions import TUISessionMixin
from .tui_session_controls import TUISessionControlMixin
from .tui_settings import TUISettingsMixin
from .tui_view_support import (
    clean_text as _clean_text,
    is_codex_binary as _is_codex_binary,
    is_pi_binary as _is_pi_binary,
    one_line as _one_line,
    render_entry as _render_entry,
    result_fields as _result_fields,
    snapshot_scalars as _snapshot_scalars,
)
def _default_backend_factory(emit: Callable[[str, str], None], **kwargs):
    """Load the execution backend only when an interactive app actually mounts."""
    from .tui_backend import TUIBackend
    return TUIBackend(emit, **kwargs)
def _default_appserver_factory(cwd: str, **kwargs):
    from .tui_adapter_appserver import CodexAppServerAdapter
    return CodexAppServerAdapter(cwd, **kwargs)
class PiBatchTUI(TUIRuntimeLifecycleMixin, TUISettingsMixin, TUISessionMixin, TUISessionControlMixin,
                 TUIApprovalMixin,
                 TUIFollowUpMixin, TUIAgentsMixin, TUIActorControlMixin,
                 TUIPipelineMixin,
                 TUIInspectorMixin,
                 TUIProductMixin, TUIReviewMixin, TUIWorkspaceMixin,
                 TUIProjectionMixin, App[None]):
    """Textual application presenting one interactive pi-batch session."""
    TITLE = "pi-batch"
    SUB_TITLE = "agent orchestration"
    COMMAND_PALETTE_BINDING = "ctrl+p"
    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Palette", priority=True),
        Binding("ctrl+n", "new_session", "New", priority=True),
        Binding("ctrl+l", "clear", "Clear", priority=True),
        Binding("ctrl+g", "show_diff", "Changes", priority=True),
        Binding("ctrl+t", "toggle_timeline", "Timeline", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]
    CSS = """
    Screen {
        layout: vertical;
    }
    Header {
        dock: top;
    }
    #workspace {
        height: 1fr;
    }
    #conversation-scroll {
        width: 1fr;
        padding: 0 2;
        scrollbar-gutter: stable;
    }
    #conversation {
        width: 100%;
        padding-bottom: 1;
    }
    #timeline { display: none; width: 100%; padding: 0 1 1 1; border-top: solid $accent; }
    #status-panel {
        width: 34;
        min-width: 24;
        padding: 1 2;
        border-left: solid $accent;
        background: $panel;
        overflow-y: auto;
    }
    #composer {
        dock: bottom;
        margin: 0 1;
        border: tall $accent;
    }
    #composer:focus {
        border: tall $accent-lighten-1;
    }
    Footer {
        dock: bottom;
    }
    """
    def __init__(
        self,
        options: Optional[TuiOptions] = None,
        *,
        backend: Any = None,
        backend_factory: Optional[Callable[..., Any]] = None,
        appserver_factory: Optional[Callable[..., Any]] = None,
        pi_rpc_factory: Optional[Callable[..., Any]] = None,
        review_runner: Optional[Callable[..., Any]] = None,
    ) -> None:
        super().__init__()
        self.options = options or TuiOptions()
        self._backend = backend
        self._managed_backend = backend is None and backend_factory is None
        self._backend_factory = backend_factory or _default_backend_factory
        self._appserver_factory = appserver_factory or _default_appserver_factory
        self._pi_rpc_factory = pi_rpc_factory or PiRpcAdapter
        self._review_runner = review_runner
        self._adapter = None
        self._adapter_session_id = ""
        self._turn_handle = self._runtime_operation = None
        self._structured_backend = False
        self._strict_adapter = False
        self._runtime_blocked_reason = ""
        self._timeline = TimelineLog()
        self._init_runtime_projection()
        self._init_approval_controls()
        self._init_follow_up_controls()
        self._init_session_controls()
        self._init_agents_inspector()
        self._init_actor_controls()
        self._timeline_mode, self._timeline_summary = "summary", ""
        self._narrow = False
        self._entries: list[TranscriptEntry] = []
        self._transcript_omitted = False
        self._assistant_index: Optional[int] = None
        self._error_index: Optional[int] = None
        self._worker = None
        self._busy = False
        self._init_review_controls()
        self._cancel_pending = False
        self._cancel_retry_count = 0
        self._turn_stdout_seen = False
        self._turn_stderr_seen = False
        self._last_status = "Starting"
        self._ui_thread_id = threading.get_ident()
        self._prompt_history: list[str] = []
        self._last_task_prompt = ""
        self._history_index = 0
        self._history_draft = ""
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="workspace"):
            with VerticalScroll(id="conversation-scroll"):
                yield Markdown(_WELCOME, id="conversation", open_links=False)
                yield Markdown("", id="timeline", open_links=False)
            yield Static("Starting…", id="status-panel", markup=False)
        yield SlashComposer(
            placeholder="Ask pi-batch…", id="composer",
            dynamic_complete=self._runtime_completion_input)
        yield Footer()
    def on_mount(self) -> None:
        self._ui_thread_id = threading.get_ident()
        try:
            if self.options.native_session:
                self._initialize_native_session_view()
            else:
                self._backend = self._resolve_backend()
                self._attach_adapter()
                snapshot = self._backend_snapshot()
                self._last_status = (
                    snapshot.get("agent_error") or "Ready"
                    if isinstance(snapshot, dict) else "Ready")
        except Exception as exc:
            self._last_status = "Backend unavailable"
            self._append_entry("error", f"Unable to initialize TUI backend: {exc}")
            self.query_one("#composer", Input).disabled = True
        self._refresh_status()
        self._refresh_timeline()
        self.query_one("#composer", Input).focus()
        self._schedule_pipeline_startup()

    def on_unmount(self) -> None:
        self._unmounting = True
        self._cancel_before_exit(wait_review=True)
        self._invalidate_runtime_completions()
        try:
            self.detach_native_pipeline()
        except (RuntimeError, TypeError):
            pass
        adapter, self._adapter = self._adapter, None
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
        try:
            self._event_controller.close()
        except Exception:
            pass
    def on_resize(self, event) -> None:
        """Keep the composer usable on the small terminals common over SSH."""
        try:
            self._narrow = event.size.width < 72
            self.query_one("#status-panel", Static).display = not self._narrow
            if self._active_timeline().entries:
                self._refresh_timeline()
        except Exception:
            pass  # resize may arrive while the screen is mounting or closing
    def _resolve_backend(self):
        candidate = self._backend
        if candidate is not None and (hasattr(candidate, "run_turn") or
                                      hasattr(candidate, "run_turn_structured")):
            setter = getattr(candidate, "set_emit", None)
            if callable(setter):
                setter(self._emit_from_backend)
            return candidate
        factory = candidate if callable(candidate) else self._backend_factory
        return factory(self._emit_from_backend, **self.options.backend_kwargs())
    def _attach_adapter(self) -> None:
        """Prefer typed runtime events when callback ownership is replaceable."""
        agent = self.options.agent or getattr(self._backend, "agent", "")
        agent, trusted_pi = self._pi_runtime_agent(agent)
        provider = str(getattr(self._backend, "provider", "") or "").strip()
        mode = str(self.options.runtime or "auto").strip().lower()
        self._runtime_blocked_reason = ""
        self._orchestrator_only_ready = False
        if self.options.orchestrator_runtime_blocked(mode):
            self._runtime_blocked_reason = (
                "Orchestrator-only mode requires the Pi RPC runtime.")
            self._append_entry("error", self._runtime_blocked_reason)
            return
        wants_pi_rpc = (mode == "pi-rpc" or self.options.orchestrator_only
                        or (mode == "auto" and self._managed_backend and trusted_pi))
        if wants_pi_rpc:
            self._attach_pi_rpc(agent, provider, trusted_pi=trusted_pi)
            return
        if mode == "app-server":
            self._attach_appserver(provider)
            return
        wants_structured = mode == "structured" or (
            mode == "auto" and _is_codex_binary(agent))
        if wants_structured and provider:
            self._runtime_blocked_reason = (
                "Provider override is unavailable for the selected Codex runtime. "
                "Clear it with `/provider \"\"` before running Codex.")
            self._append_entry("error", self._runtime_blocked_reason)
            return
        if wants_structured:
            if self._attach_structured_backend(agent):
                return
            if mode == "structured":
                self._runtime_blocked_reason = (
                    "Structured runtime requires a Codex agent and a structured backend.")
                self._append_entry("error", self._runtime_blocked_reason)
                return
        self._attach_legacy_adapter()
    def _attach_pi_rpc(self, agent: str, provider: str, *, trusted_pi=False) -> None:
        if not (trusted_pi or _is_pi_binary(agent)):
            self._runtime_blocked_reason = (
                "Pi RPC runtime requires the canonical Pi executable.")
            self._append_entry("error", self._runtime_blocked_reason)
            return
        adapter = None
        try:
            kwargs = {}
            if self.options.orchestrator_only:
                no_tools_args = config.agent_no_tools_args(agent)
                if not no_tools_args:
                    raise RuntimeError(
                        "no-tools adapter arguments are not configured")
                kwargs["no_tools_args"] = no_tools_args
            adapter = self._pi_rpc_factory(
                self._session_cwd(), agent=agent,
                model=str(self.options.model or ""), provider=provider,
                session_name=self.options.session_name, **kwargs)
            if (self.options.orchestrator_only
                    and getattr(adapter, "tools_disabled", False) is not True):
                raise RuntimeError("Pi RPC adapter did not confirm tools are disabled")
            adapter.initialize({})
            session = adapter.start_session({
                "name": self.options.session_name,
                "workspace_id": self._session_cwd(),
            })
        except Exception as exc:
            if adapter is not None:
                adapter.close()
            self._runtime_blocked_reason = f"Pi RPC unavailable: {exc}"
            self._append_entry("error", self._runtime_blocked_reason)
            return
        self._adapter = adapter
        self._adapter_session_id = str(session.session_id)
        self._adapter_workspace_id = str(session.workspace_id)
        self._strict_adapter = True
        self._orchestrator_only_ready = bool(self.options.orchestrator_only)
        self._enable_runtime_projection()
    def _attach_appserver(self, provider: str) -> None:
        if provider:
            self._runtime_blocked_reason = (
                "Provider override is unavailable for Codex App Server. "
                "Clear it with `/provider \"\"` before connecting.")
            self._append_entry("error", self._runtime_blocked_reason)
            return
        adapter = None
        try:
            startup = self.options.startup_timeout
            kwargs = {} if startup is None else {"startup_timeout": startup}
            adapter = self._appserver_factory(self._session_cwd(), **kwargs)
            adapter.initialize({})
            session = adapter.start_session({
                "name": self.options.session_name, "model": self.options.model})
        except Exception as exc:
            if adapter is not None:
                adapter.close()
            self._runtime_blocked_reason = f"Codex App Server unavailable: {exc}"
            self._append_entry("error", self._runtime_blocked_reason)
            return
        self._adapter = adapter
        self._adapter_session_id = str(session.session_id)
        self._adapter_workspace_id = str(session.workspace_id)
        self._strict_adapter = True
        self._enable_runtime_projection()
    def _attach_structured_backend(self, agent: str) -> bool:
        if (not _is_codex_binary(agent) or not
                callable(getattr(self._backend, "run_turn_structured", None))):
            return False
        self._structured_backend = True
        event_setter = getattr(self._backend, "set_runtime_event_sink", None)
        if callable(event_setter):
            event_setter(self._ingest_runtime_event)
            self._enable_runtime_projection()
        setter = getattr(self._backend, "set_emit", None)
        if callable(setter):
            setter(self._emit_from_backend)
        return True

    def _attach_legacy_adapter(self) -> None:
        if not callable(getattr(self._backend, "set_emit", None)):
            self._backend.emit = self._emit_from_backend
            return
        adapter = None
        try:
            from .tui_adapter_legacy import LegacyAdapter

            adapter = LegacyAdapter(self._backend)
            session = adapter._adopt_session({"name": self.options.session_name})
        except Exception:
            if adapter is not None:
                adapter.close()
            setter = getattr(self._backend, "set_emit", None)
            if callable(setter):
                setter(self._emit_from_backend)
            else:
                try:
                    self._backend.emit = self._emit_from_backend
                except (AttributeError, TypeError):
                    pass
            return
        self._adapter = adapter
        self._adapter_session_id = str(session.session_id)
        self._adapter_workspace_id = str(session.workspace_id)
        self._strict_adapter = False
        self._enable_runtime_projection()
    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        for spec in palette_specs():
            yield SystemCommand(
                spec.palette_title, spec.palette_description,
                palette_callback(self, spec))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        event.input.value = ""
        if not value:
            return
        self._remember_input(value)
        invocation = parse_slash_command(value)
        if self._handle_busy_submission(value, invocation):
            return
        if invocation is not None:
            if self._dispatch_slash(invocation) is False:
                event.input.value = value
            return
        if not self._start_turn(value):
            event.input.value = value

    def _dispatch_slash(self, command: SlashInvocation) -> Optional[bool]:
        if command.error:
            self._append_entry("error", f"Invalid slash command: {command.error}")
            return
        spec = command_spec(command.name)
        if spec is None:
            self._append_entry(
                "error", f"Unknown slash command `/{command.name}`. Use `/help` for commands.")
            return
        return getattr(self, spec.handler_name)(command.args)

    def _start_turn(self, prompt: str) -> bool:
        if self._busy or self._session_control_busy():
            self.notify("Wait for the active turn or session compaction first.",
                        severity="warning")
            return False
        if self._native_pipeline_is_running():
            self.notify("Wait for the attached native pipeline to finish.",
                        severity="warning")
            return False
        if self._native_pipeline_attachment is not None:
            self.detach_native_pipeline()
        if self._backend is None:
            self._append_entry("error", "The TUI backend is unavailable.")
            return False
        if self._runtime_blocked_reason:
            self._append_entry("error", self._runtime_blocked_reason)
            return False
        if not self._prepare_runtime_action():
            return False
        self._last_task_prompt = prompt
        self._append_entry("user", prompt, render=False)
        self._assistant_index = len(self._entries)
        self._entries.append(TranscriptEntry("assistant", ""))
        self._trim_transcript()
        self._error_index = None
        self._turn_stdout_seen = False
        self._turn_stderr_seen = False
        self._timeline = TimelineLog()
        self._cancel_pending = False
        self._cancel_retry_count = 0
        self._timeline_mode, self._timeline_summary = "summary", ""
        self._refresh_timeline()
        self._set_busy(True)
        self._begin_runtime_operation("Agent turn")
        self._mark_agent_operation_started("turn", prompt)
        self._render_conversation()
        self._worker = self.run_worker(
            lambda: self._run_turn_worker(prompt),
            name="agent-turn",
            group="agent",
            exit_on_error=False,
            exclusive=True,
            thread=True,
        )
        return True

    def _run_backend_turn(self, prompt: str) -> tuple[Any, Optional[Exception]]:
        try:
            method = (self._backend.run_turn_structured if self._structured_backend
                      else self._backend.run_turn)
            return method(prompt), None
        except Exception as exc:  # backend errors are rendered, never crash the UI
            return None, exc

    def _disable_adapter(self, adapter: Any) -> None:
        try:
            adapter.close()
        finally:
            self._adapter = None
            self._adapter_session_id = ""
            self._adapter_workspace_id = ""
            self._strict_adapter = False
            self._orchestrator_only_ready = False
            self._disable_runtime_projection()
            setter = getattr(self._backend, "set_emit", None)
            if callable(setter):
                setter(self._emit_from_backend)
            else:
                try:
                    self._backend.emit = self._emit_from_backend
                except (AttributeError, TypeError):
                    pass

    def _emit_from_backend(self, kind: str, text: str) -> None:
        """Backend callback; all worker-thread UI mutation crosses this bridge."""
        callback = self._apply_backend_emit
        if threading.get_ident() == self._ui_thread_id:
            callback(kind, text)
            return
        try:
            self.call_from_thread(callback, kind, text)
        except RuntimeError:
            pass  # app is shutting down

    def _apply_backend_emit(self, kind: str, text: str) -> None:
        clean = _clean_text(text)
        normalized = str(kind or "status").lower()
        if self._projection_owns_callback(normalized):
            return
        timeline_name = "stdout" if normalized == "stdout-replace" else normalized
        timeline_kind = timeline_name in {"command", "command-output", "file",
                                          "plan", "usage", "stdout", "stderr", "status"}
        if self._structured_backend and not self._projection_driven and timeline_kind:
            self._timeline.consume(timeline_name, clean)
            self._refresh_timeline()
            self._refresh_plan_inspector()
        if normalized in {"command", "command-output", "file", "plan", "usage"}:
            return
        if normalized in {"stdout", "stdout-replace"}:
            self._apply_stdout_callback(
                clean, replace=normalized == "stdout-replace")
        elif normalized == "stderr":
            self._turn_stderr_seen = self._turn_stderr_seen or bool(clean)
            self._append_active("error", clean)
        else:
            self._last_status = _one_line(clean) or "Running"
            self._refresh_status()

    def _append_active(self, role: str, text: str) -> None:
        if not text:
            return
        index_name = "_assistant_index" if role == "assistant" else "_error_index"
        index = getattr(self, index_name)
        if index is None:
            index = len(self._entries)
            self._entries.append(TranscriptEntry(role, ""))
            setattr(self, index_name, index)
        self._entries[index].text += text
        self._trim_transcript()
        self._render_conversation()

    def _finish_turn(self, result: Any, error: Optional[Exception]) -> None:
        self._apply_runtime_finish()
        if error is not None:
            self._finish_turn_error(error)
        else:
            self._finish_turn_result(result)
        timeline = self._active_timeline()
        if (self._structured_backend or self._projection_driven) and timeline.entries:
            self._timeline_summary = self._active_timeline_summary()
        self._append_workspace_change()
        if callable(getattr(self, "_collect_review_fix_evidence", None)):
            self._collect_review_fix_evidence()
        self._assistant_index = None
        self._error_index = None
        self._worker = None
        self._cancel_pending = False
        self._set_busy(False)
        self._mark_agent_operation_finished()
        self._refresh_completion_inspector()
        self._complete_pipeline_handoff_turn(result, error)
        self._schedule_follow_up_after_turn(result, error)

    def _finish_turn_error(self, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        self._reconcile_runtime_failure(message)
        if not self._turn_stderr_seen:
            self._append_active("error", message)
        self._last_status = self._projected_terminal_label() or "Failed"

    def _finish_turn_result(self, result: Any) -> None:
        success, stdout, stderr, reason = _result_fields(result)
        if not success:
            self._reconcile_runtime_failure(reason or stderr)
        if stdout and not self._turn_stdout_seen:
            self._append_active("assistant", stdout)
        if stderr and not self._turn_stderr_seen:
            self._append_active("error", stderr)
        if (not success and reason and reason != stderr
                and not self._turn_stderr_seen):
            self._append_active("error", reason)
        fallback = "Ready" if success else "Failed"
        self._last_status = self._projected_terminal_label() or fallback

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self._invalidate_runtime_completions()
        self._busy = busy
        composer = self.query_one("#composer", Input)
        self._configure_follow_up_composer(composer, busy)
        self._refresh_status()
        if not busy:
            self.call_after_refresh(composer.focus)

    def _append_entry(self, role: str, text: str, *, render: bool = True) -> None:
        self._entries.append(TranscriptEntry(role, _clean_text(text)))
        self._trim_transcript()
        if render and self.is_mounted:
            self._render_conversation()

    def _trim_transcript(self) -> None:
        result = trim_transcript(
            self._entries, (self._assistant_index, self._error_index))
        self._assistant_index, self._error_index = result.protected_indexes
        self._transcript_omitted = self._transcript_omitted or result.omitted

    def _render_conversation(self) -> None:
        document = [_WELCOME.rstrip()]
        if self._transcript_omitted:
            document.append(OMISSION_MARKDOWN)
        for entry in self._entries:
            document.append(_render_entry(entry, self._busy))
        self.query_one("#conversation", Markdown).update("\n\n".join(document) + "\n")
        scroll = self.query_one("#conversation-scroll", VerticalScroll)
        self.call_after_refresh(scroll.scroll_end, animate=False, x_axis=False)

    def _refresh_status(self) -> None:
        snapshot = self._backend_snapshot()
        fallback = "busy" if self._busy else "ready"
        lines = ["STATUS", "", f"state     {self._projection_status_state(fallback)}"]
        lines.extend(self._option_status_lines(snapshot))
        lines.extend(self._session_control_status_lines())
        lines.extend(self._follow_up_status_lines())
        lines.extend(self._approval_status_lines())
        viewed = self._runtime_view.session_id if self._projection_driven else ""
        active_session = str(self._adapter_session_id or
                             snapshot.get("session_id", "") or "")
        viewing_history = bool(viewed and active_session and viewed != active_session)
        if viewing_history:
            lines.append(f"session   {_one_line(viewed)[:20]} (history)")
        hidden = {"state", "agent", "model", "provider", "memory_mode", "cwd"}
        for key, value in _snapshot_scalars(snapshot):
            if key not in hidden and not (viewing_history and key == "session_id"):
                lines.append(f"{key[:9]:<9} {_one_line(str(value))[:20]}")
        checkpoint = self._native_pipeline_checkpoint_status_line()
        for detail in (self._native_pipeline_recovery_status_line(), checkpoint):
            if detail:
                lines.append(detail)
        if self._last_status:
            lines.extend(["", _one_line(self._last_status)[:120]])
        if self._timeline_summary:
            lines.append(_one_line(self._timeline_summary)[:120])
        self.query_one("#status-panel", Static).update("\n".join(lines))

    def _refresh_timeline(self) -> None:
        widget = self.query_one("#timeline", Markdown)
        timeline = self._active_timeline()
        visible = bool(timeline.entries and not self._narrow and
                       self._timeline_mode != "hidden")
        if widget.display != visible:
            widget.display = visible
        if not visible:
            return
        width = max(20, self.size.width - 42)
        lines = (timeline.render_lines(width) if self._timeline_mode == "expanded"
                 else [timeline.render_summary(width)])
        widget.update("### Timeline\n\n" + "\n".join(f"    {line}" for line in lines))

    def _backend_snapshot(self) -> dict[str, Any]:
        if self._backend is None:
            return {}
        snapshot = getattr(self._backend, "snapshot", None)
        snapshot = snapshot if callable(snapshot) else getattr(self._backend, "status", None)
        if not callable(snapshot):
            return {}
        try:
            value = snapshot()
        except Exception as exc:
            return {"backend": f"snapshot failed: {exc}"}
        return value if isinstance(value, dict) else {"backend": value}

    def _slash_new(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._usage("new", "[name]")
            return
        self._new_session(args[0] if args else "")

    def _slash_clear(self, args: tuple[str, ...]) -> None:
        if self._require_no_args("clear", args):
            self.action_clear()

    def _slash_breaker(self, args: tuple[str, ...] = ()) -> None:
        if not self._require_no_args("breaker", args):
            return
        from . import config
        from .tui_agents import render_breaker_lines
        lines = render_breaker_lines(
            config.data_dir(self.options.cwd), max(40, self.size.width - 4))
        self._append_entry("system", "\n".join(lines))

    def _slash_doctor(self, args: tuple[str, ...]) -> None:
        if not self._require_no_args("doctor", args):
            return
        try:
            from .product_cli import _doctor_checks
            checks = _doctor_checks()
            lines = [f"- {'✅' if ok else '⚠️'} **{name}** — {detail}"
                     for name, ok, detail in checks]
            self._append_entry("system", "## Doctor\n\n" + "\n".join(lines))
        except Exception as exc:
            self._append_entry("error", f"Doctor failed: {exc}")
    def _slash_quit(self, args: tuple[str, ...]) -> None:
        if self._require_no_args("quit", args):
            self.action_quit()
    def _slash_timeline(self, args: tuple[str, ...]) -> None:
        if self._require_no_args("timeline", args):
            self.action_toggle_timeline()
    def _require_no_args(self, command: str, args: tuple[str, ...]) -> bool:
        if args:
            self._usage(command, "")
            return False
        return True
    def _usage(self, command: str, suffix: str) -> None:
        usage = f"/{command}" + (f" {suffix}" if suffix else "")
        self._append_entry("error", f"Usage: `{usage}`")
    def _new_session(self, name: str = "") -> None:
        if self._busy or self._session_control_busy():
            self.notify("Wait for the active turn or session compaction first.",
                        severity="warning")
            return
        adapter_switch_attempted = False
        try:
            if self._restart_stopped_runtime():
                snapshot = self._backend_snapshot()
            elif self._adapter is None:
                snapshot = self._backend.new_session(name)
            else:
                self._prepare_runtime_session_switch()
                target_session_id = f"ses_{uuid.uuid4().hex}"
                self._preflight_runtime_session(target_session_id)
                adapter_switch_attempted = True
                session = self._adapter.start_session({
                    "name": name, "session_id": target_session_id,
                    "model": self._option_value("model")})
                self._adapter_session_id = str(session.session_id)
                self._adapter_workspace_id = str(session.workspace_id)
                snapshot = self._backend_snapshot()
            self._prepare_runtime_projection()
            self._reset_follow_ups_for_session_switch()
        except Exception as exc:
            if adapter_switch_attempted:
                self._runtime_blocked_reason = (
                    "Runtime session switch did not complete; start another "
                    "new session before running a turn.")
            self._append_entry("error", f"Unable to start a new session: {exc}")
            return
        self._entries.clear()
        self._review_findings = None
        self._last_task_prompt = ""
        self._transcript_omitted = False
        self._assistant_index = None
        self._error_index = None
        label = snapshot.get("session_name", "") if isinstance(snapshot, dict) else ""
        self._append_entry("system", f"New session started{f': `{label}`' if label else '.'}")
        self._last_status = "New session"
        self._refresh_status()

    def action_new_session(self) -> None:
        self._new_session()

    def action_clear(self) -> None:
        self._projection_clear_visible()
        self._entries.clear()
        self._transcript_omitted = False
        self._assistant_index = None
        self._error_index = None
        self._render_conversation()
        self._last_status = "Conversation cleared"
        self._refresh_status()

    def action_toggle_timeline(self) -> None:
        self._timeline_mode = ("hidden" if self._timeline_mode == "expanded"
                               else "expanded")
        self._refresh_timeline()

    def action_cancel(self) -> None:
        if self._handle_runtime_setting_cancel():
            return
        if self._session_compaction_running():
            self.notify("Session compaction cannot be safely cancelled.",
                        severity="warning")
            return
        if self._review_busy:
            self._cancel_review()
            return
        if getattr(self, "_pipeline_busy", False):
            try:
                accepted = self._request_runtime_cancel()
            except Exception as exc:
                self._append_entry("error", f"Pipeline cancel failed: {exc}")
                return
            if accepted:
                self._last_status = "Pipeline cancellation requested"
                self._refresh_status()
            else:
                self.notify(
                    "Pipeline cancellation is unavailable for this run.",
                    severity="warning")
            return
        if not self._busy or self._backend is None:
            if self._cancel_persistent_pipeline():
                return
            self.notify("No active turn to cancel.")
            return
        try:
            accepted = self._request_runtime_cancel()
        except Exception as exc:
            self._append_entry("error", f"Cancel failed: {exc}")
            return
        self._cancel_pending = not accepted
        self._cancel_retry_count = 0
        if self._cancel_pending:
            self.set_timer(0.01, self._retry_pending_cancel)
        self._last_status = (
            "Cancellation requested" if accepted else "Cancellation pending")
        self._refresh_status()

    def _request_runtime_cancel(self) -> bool:
        if getattr(self, "_pipeline_busy", False):
            channel = getattr(self, "_native_pipeline_approval", None)
            view = getattr(self, "_runtime_view", None)
            scope = {
                "workspace_id": getattr(view, "workspace_id", ""),
                "session_id": getattr(view, "session_id", ""),
                "run_id": getattr(view, "run_id", ""),
                "turn_id": getattr(view, "turn_id", ""),
                "actor_id": getattr(view, "actor_id", ""),
            }
            request_cancel = getattr(channel, "request_cancel", None)
            return bool(callable(request_cancel) and request_cancel(scope))
        handle = self._turn_handle
        if self._runtime_operation is not None:
            return self._runtime_operation.cancel()
        if self._adapter is not None and handle is not None:
            self._adapter.interrupt_turn({"turn_id": handle.turn_id})
            return True
        return bool(self._backend.cancel())
    def _retry_pending_cancel(self) -> None:
        if not self._busy or not self._cancel_pending:
            return
        try:
            accepted = self._request_runtime_cancel()
        except Exception as exc:
            self._cancel_pending = False
            self._append_entry("error", f"Cancel failed: {exc}")
            return
        if accepted:
            self._cancel_pending = False
            self._last_status = "Cancellation requested"
            self._refresh_status()
            return
        self._cancel_retry_count += 1
        if self._cancel_retry_count < 100:
            self.set_timer(0.01, self._retry_pending_cancel)

    def action_quit(self) -> None:
        self._cancel_before_exit()
        if self._worker is not None:
            self._worker.cancel()
        self._cancel_before_exit()
        self.exit()

    def _cancel_before_exit(self, *, wait_review: bool = False) -> None:
        """Cover the race where a submitted worker starts while TUI exits."""
        if self._review_busy:
            self._cancel_review(wait=wait_review)
            return
        if getattr(self, "_pipeline_busy", False):
            try:
                self._request_runtime_cancel()
            except Exception:
                pass
            return
        if not self._busy or self._backend is None:
            return
        try:
            self._request_runtime_cancel()
        except Exception:
            pass

    def _palette_help(self) -> None:
        self._slash_help(())
    def _palette_status(self) -> None:
        self._slash_status(())
    def _palette_doctor(self) -> None:
        self._slash_doctor(())

    def _palette_review(self) -> None:
        self._slash_review(())

    def on_key(self, event: events.Key) -> None:
        if event.key not in {"up", "down"} or self._busy:
            return
        composer = self.query_one("#composer", Input)
        if self.focused is not composer or not self._prompt_history:
            return
        self._navigate_history(-1 if event.key == "up" else 1, composer)
        event.prevent_default()
        event.stop()

    def _remember_input(self, value: str) -> None:
        if not self._prompt_history or self._prompt_history[-1] != value:
            self._prompt_history.append(value)
            del self._prompt_history[:-100]
        self._history_index = len(self._prompt_history)
        self._history_draft = ""

    def _navigate_history(self, direction: int, composer: Input) -> None:
        end = len(self._prompt_history)
        if self._history_index == end and direction < 0:
            self._history_draft = composer.value
        self._history_index = max(0, min(end, self._history_index + direction))
        composer.value = (self._history_draft if self._history_index == end
                          else self._prompt_history[self._history_index])
        composer.cursor_position = len(composer.value)

TUIApplication = PiBatchTUI

def _build_parser():
    from .tui_cli_options import build_parser
    return build_parser()


def _options_from_args(parser, args):
    from .tui_cli_options import options_from_args
    return options_from_args(parser, args)


def _interactive_terminal() -> bool:
    capable = os.environ.get("TERM", "").lower() != "dumb"
    return bool(capable and getattr(sys.stdin, "isatty", lambda: False)()
                and getattr(sys.stdout, "isatty", lambda: False)())

def tui_main(
    argv: Optional[list[str]] = None,
    *,
    backend: Any = None,
    backend_factory: Optional[Callable[..., Any]] = None,
    appserver_factory: Optional[Callable[..., Any]] = None,
    pi_rpc_factory: Optional[Callable[..., Any]] = None,
) -> int:
    """Parse TUI options and run interactively; non-TTY execution exits 2."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    options = _options_from_args(parser, args)
    if not _interactive_terminal():
        print("pi-batch tui requires interactive stdin and stdout terminals", file=sys.stderr)
        return 2
    app = PiBatchTUI(
        options, backend=backend, backend_factory=backend_factory,
        appserver_factory=appserver_factory, pi_rpc_factory=pi_rpc_factory)
    return app.run_interactive()

if __name__ == "__main__":
    raise SystemExit(tui_main())
