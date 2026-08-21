"""Product terminal UI for interactive pi-batch sessions.

The UI deliberately talks to the execution layer through one small backend
adapter.  Importing this module is therefore safe for ``tui --help`` and for
tests which inject a fake backend; the real :class:`TUIBackend` is imported
only when the application mounts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Markdown, Static


_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_SLASH_HELP = """\
Available commands:

- `/help` — show this help
- `/status` — show the current backend/session snapshot
- `/new` — start a new backend session
- `/clear` — clear the visible conversation only
- `/model [name]` — show or change the model
- `/provider [name]` — show or change the provider
- `/agent [binary]` — show or change the agent CLI
- `/memory auto|on|off` — show or change memory mode
- `/doctor` — run local environment diagnostics
- `/quit` — cancel active work and leave the TUI

Text without a leading `/` is sent to the agent.
"""
_WELCOME = """\
# pi-batch

Describe the work you want to run. Use `/help` for commands or **Ctrl+P** for
the command palette.
"""


@dataclass
class TuiOptions:
    """Backend options supplied by ``tui_main`` and changed by slash commands."""

    cwd: str = ""
    agent: str = ""
    model: str = ""
    provider: str = ""
    memory_mode: str = ""
    timeout: int = 0
    session_name: str = "tui"

    def backend_kwargs(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "agent": self.agent,
            "model": self.model,
            "provider": self.provider,
            "memory_mode": self.memory_mode,
            "timeout": self.timeout,
            "session_name": self.session_name,
        }


@dataclass(frozen=True)
class SlashInvocation:
    """A parsed slash command. ``error`` is set for malformed quoting."""

    name: str
    args: tuple[str, ...] = ()
    error: str = ""


@dataclass
class TranscriptEntry:
    role: str
    text: str


def parse_slash_command(text: str) -> Optional[SlashInvocation]:
    """Parse a slash command without ever treating malformed input as a prompt."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    try:
        tokens = shlex.split(stripped[1:])
    except ValueError as exc:
        return SlashInvocation("", error=str(exc))
    if not tokens:
        return SlashInvocation("help")
    return SlashInvocation(tokens[0].lower(), tuple(tokens[1:]))


def _default_backend_factory(emit: Callable[[str, str], None], **kwargs):
    """Load the execution backend only when an interactive app actually mounts."""
    from .tui_backend import TUIBackend

    return TUIBackend(emit, **kwargs)


class PiBatchTUI(App[None]):
    """Textual application presenting one interactive pi-batch session."""

    TITLE = "pi-batch"
    SUB_TITLE = "agent orchestration"
    COMMAND_PALETTE_BINDING = "ctrl+p"
    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Palette", priority=True),
        Binding("ctrl+n", "new_session", "New", priority=True),
        Binding("ctrl+l", "clear", "Clear", priority=True),
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
    ) -> None:
        super().__init__()
        self.options = options or TuiOptions()
        self._backend = backend
        self._backend_factory = backend_factory or _default_backend_factory
        self._entries: list[TranscriptEntry] = []
        self._assistant_index: Optional[int] = None
        self._error_index: Optional[int] = None
        self._worker = None
        self._busy = False
        self._turn_stdout_seen = False
        self._turn_stderr_seen = False
        self._last_status = "Starting"
        self._ui_thread_id = threading.get_ident()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="workspace"):
            with VerticalScroll(id="conversation-scroll"):
                yield Markdown(_WELCOME, id="conversation", open_links=False)
            yield Static("Starting…", id="status-panel", markup=False)
        yield Input(placeholder="Ask pi-batch…", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        self._ui_thread_id = threading.get_ident()
        try:
            self._backend = self._resolve_backend()
            snapshot = self._backend_snapshot()
            self._last_status = (
                snapshot.get("agent_error") or "Ready"
                if isinstance(snapshot, dict) else "Ready")
        except Exception as exc:
            self._last_status = "Backend unavailable"
            self._append_entry("error", f"Unable to initialize TUI backend: {exc}")
            self.query_one("#composer", Input).disabled = True
        self._refresh_status()
        self.query_one("#composer", Input).focus()

    def on_resize(self, event) -> None:
        """Keep the composer usable on the small terminals common over SSH."""
        try:
            self.query_one("#status-panel", Static).display = event.size.width >= 72
        except Exception:
            pass  # resize may arrive while the screen is mounting or closing

    def _resolve_backend(self):
        candidate = self._backend
        if candidate is not None and hasattr(candidate, "run_turn"):
            setter = getattr(candidate, "set_emit", None)
            if callable(setter):
                setter(self._emit_from_backend)
            return candidate
        factory = candidate if callable(candidate) else self._backend_factory
        return factory(self._emit_from_backend, **self.options.backend_kwargs())

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand("New session", "Reset backend session", self.action_new_session)
        yield SystemCommand("Clear conversation", "Clear visible messages", self.action_clear)
        yield SystemCommand("Show status", "Display backend snapshot", self._palette_status)
        yield SystemCommand("Run doctor", "Check local environment", self._palette_doctor)
        yield SystemCommand("Slash command help", "List TUI commands", self._palette_help)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        event.input.value = ""
        if not value:
            return
        if self._busy:
            self.notify("A turn is already running; cancel it before submitting again.",
                        severity="warning")
            return
        invocation = parse_slash_command(value)
        if invocation is not None:
            self._dispatch_slash(invocation)
            return
        self._start_turn(value)

    def _dispatch_slash(self, command: SlashInvocation) -> None:
        if command.error:
            self._append_entry("error", f"Invalid slash command: {command.error}")
            return
        handlers = {
            "help": self._slash_help,
            "status": self._slash_status,
            "new": self._slash_new,
            "clear": self._slash_clear,
            "model": self._slash_model,
            "provider": self._slash_provider,
            "agent": self._slash_agent,
            "memory": self._slash_memory,
            "doctor": self._slash_doctor,
            "quit": self._slash_quit,
        }
        handler = handlers.get(command.name)
        if handler is None:
            self._append_entry(
                "error", f"Unknown slash command `/{command.name}`. Use `/help` for commands.")
            return
        handler(command.args)

    def _start_turn(self, prompt: str) -> None:
        if self._busy:
            self.notify("A turn is already running; cancel it before submitting again.",
                        severity="warning")
            return
        if self._backend is None:
            self._append_entry("error", "The TUI backend is unavailable.")
            return
        self._append_entry("user", prompt, render=False)
        self._assistant_index = len(self._entries)
        self._entries.append(TranscriptEntry("assistant", ""))
        self._error_index = None
        self._turn_stdout_seen = False
        self._turn_stderr_seen = False
        self._set_busy(True)
        self._render_conversation()
        self._worker = self.run_worker(
            lambda: self._run_turn_worker(prompt),
            name="agent-turn",
            group="agent",
            exit_on_error=False,
            exclusive=True,
            thread=True,
        )

    def _run_turn_worker(self, prompt: str) -> None:
        result, error = None, None
        try:
            result = self._backend.run_turn(prompt)
        except Exception as exc:  # backend errors are rendered, never crash the UI
            error = exc
        try:
            self.call_from_thread(self._finish_turn, result, error)
        except RuntimeError:
            pass  # application closed while the backend was unwinding

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
        if normalized == "stdout":
            self._turn_stdout_seen = self._turn_stdout_seen or bool(clean)
            self._append_active("assistant", clean)
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
        self._render_conversation()

    def _finish_turn(self, result: Any, error: Optional[Exception]) -> None:
        if error is not None:
            self._append_active("error", f"{type(error).__name__}: {error}")
            self._last_status = "Failed"
        else:
            success, stdout, stderr, reason = _result_fields(result)
            if stdout and not self._turn_stdout_seen:
                self._append_active("assistant", stdout)
            if stderr and not self._turn_stderr_seen:
                self._append_active("error", stderr)
            if not success and reason:
                self._append_active("error", reason)
            self._last_status = "Ready" if success else "Failed"
        self._assistant_index = None
        self._error_index = None
        self._worker = None
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        composer = self.query_one("#composer", Input)
        composer.disabled = busy
        composer.placeholder = "Agent is working… Ctrl+C to cancel" if busy else "Ask pi-batch…"
        self._refresh_status()
        if not busy:
            self.call_after_refresh(composer.focus)

    def _append_entry(self, role: str, text: str, *, render: bool = True) -> None:
        self._entries.append(TranscriptEntry(role, _clean_text(text)))
        if render and self.is_mounted:
            self._render_conversation()

    def _render_conversation(self) -> None:
        document = [_WELCOME.rstrip()]
        for entry in self._entries:
            document.append(_render_entry(entry, self._busy))
        self.query_one("#conversation", Markdown).update("\n\n".join(document) + "\n")
        scroll = self.query_one("#conversation-scroll", VerticalScroll)
        self.call_after_refresh(scroll.scroll_end, animate=False, x_axis=False)

    def _refresh_status(self) -> None:
        snapshot = self._backend_snapshot()
        lines = ["STATUS", "", f"state     {'busy' if self._busy else 'ready'}"]
        lines.extend(self._option_status_lines())
        for key, value in _snapshot_scalars(snapshot):
            if key not in {"state", "agent", "model", "provider", "memory_mode", "cwd"}:
                lines.append(f"{key[:9]:<9} {_one_line(str(value))[:20]}")
        if self._last_status:
            lines.extend(["", _one_line(self._last_status)[:120]])
        self.query_one("#status-panel", Static).update("\n".join(lines))

    def _option_status_lines(self) -> list[str]:
        cwd = self.options.cwd or os.getcwd()
        return [
            f"workspace {_short_path(cwd, 20)}",
            f"agent     {self._option_value('agent') or 'default'}",
            f"model     {self._option_value('model') or 'default'}",
            f"provider  {self._option_value('provider') or 'default'}",
            f"memory    {self._option_value('memory_mode') or 'auto'}",
        ]

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

    def _option_value(self, name: str) -> Any:
        if self._backend is not None and hasattr(self._backend, name):
            return getattr(self._backend, name)
        backend_options = getattr(self._backend, "options", None)
        if isinstance(backend_options, dict) and name in backend_options:
            return backend_options[name]
        return getattr(self.options, name)

    def _set_option(self, name: str, value: str) -> None:
        setattr(self.options, name, value)
        if self._backend is not None:
            try:
                setattr(self._backend, name, value)
                backend_options = getattr(self._backend, "options", None)
                if isinstance(backend_options, dict):
                    backend_options[name] = value
            except (AttributeError, TypeError) as exc:
                self._append_entry("error", f"Unable to set {name}: {exc}")
                return
        self._last_status = f"{name} updated; applies to the next turn"
        self._refresh_status()
        self._append_entry("system", f"`{name}` set to `{value or 'default'}`.")

    def _slash_help(self, args: tuple[str, ...]) -> None:
        if self._require_no_args("help", args):
            self._append_entry("system", _SLASH_HELP)

    def _slash_status(self, args: tuple[str, ...]) -> None:
        if not self._require_no_args("status", args):
            return
        payload = self._backend_snapshot()
        self._append_entry("system", "```json\n" + json.dumps(
            payload, ensure_ascii=False, indent=2, default=str) + "\n```")
        self._refresh_status()

    def _slash_new(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._usage("new", "[name]")
            return
        self._new_session(args[0] if args else "")

    def _slash_clear(self, args: tuple[str, ...]) -> None:
        if self._require_no_args("clear", args):
            self.action_clear()

    def _slash_model(self, args: tuple[str, ...]) -> None:
        self._show_or_set("model", args)

    def _slash_provider(self, args: tuple[str, ...]) -> None:
        self._show_or_set("provider", args)

    def _slash_agent(self, args: tuple[str, ...]) -> None:
        self._show_or_set("agent", args)

    def _slash_memory(self, args: tuple[str, ...]) -> None:
        if not args:
            self._append_entry("system", f"`memory_mode`: `{self._option_value('memory_mode')}`")
            return
        if len(args) != 1 or args[0] not in {"auto", "on", "off"}:
            self._usage("memory", "auto|on|off")
            return
        self._set_option("memory_mode", args[0])

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

    def _show_or_set(self, name: str, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._usage(name, "[value]")
        elif not args:
            self._append_entry("system", f"`{name}`: `{self._option_value(name) or 'default'}`")
        else:
            self._set_option(name, args[0])

    def _require_no_args(self, command: str, args: tuple[str, ...]) -> bool:
        if args:
            self._usage(command, "")
            return False
        return True

    def _usage(self, command: str, suffix: str) -> None:
        usage = f"/{command}" + (f" {suffix}" if suffix else "")
        self._append_entry("error", f"Usage: `{usage}`")

    def _new_session(self, name: str = "") -> None:
        if self._busy:
            self.notify("Cancel the active turn before starting a new session.",
                        severity="warning")
            return
        try:
            snapshot = self._backend.new_session(name)
        except Exception as exc:
            self._append_entry("error", f"Unable to start a new session: {exc}")
            return
        self._entries.clear()
        label = snapshot.get("session_name", "") if isinstance(snapshot, dict) else ""
        self._append_entry("system", f"New session started{f': `{label}`' if label else '.'}")
        self._last_status = "New session"
        self._refresh_status()

    def action_new_session(self) -> None:
        self._new_session()

    def action_clear(self) -> None:
        self._entries.clear()
        self._assistant_index = None
        self._error_index = None
        self._render_conversation()
        self._last_status = "Conversation cleared"
        self._refresh_status()

    def action_cancel(self) -> None:
        if not self._busy or self._backend is None:
            self.notify("No active turn to cancel.")
            return
        try:
            accepted = bool(self._backend.cancel())
        except Exception as exc:
            self._append_entry("error", f"Cancel failed: {exc}")
            return
        if self._worker is not None:
            self._worker.cancel()
        self._last_status = "Cancellation requested" if accepted else "Nothing cancelled"
        self._refresh_status()

    def action_quit(self) -> None:
        if self._busy and self._backend is not None:
            try:
                self._backend.cancel()
            except Exception:
                pass
        if self._worker is not None:
            self._worker.cancel()
        self.exit()

    def _palette_help(self) -> None:
        self._slash_help(())

    def _palette_status(self) -> None:
        self._slash_status(())

    def _palette_doctor(self) -> None:
        self._slash_doctor(())


# A descriptive alias for embedding code which doesn't want product branding.
TUIApplication = PiBatchTUI


def _clean_text(value: Any) -> str:
    return _ANSI_ESCAPE.sub("", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _short_path(value: str, width: int) -> str:
    path = str(value)
    return path if len(path) <= width else "…" + path[-(width - 1):]


def _render_entry(entry: TranscriptEntry, busy: bool) -> str:
    labels = {"user": "You", "assistant": "Assistant", "system": "System", "error": "Error"}
    body = entry.text
    if entry.role == "assistant" and not body:
        body = "_Working…_" if busy else "_No textual response._"
    if entry.role == "error":
        body = "\n".join(f"> {line}" for line in body.splitlines())
    return f"## {labels.get(entry.role, entry.role.title())}\n\n{body}"


def _result_fields(result: Any) -> tuple[bool, str, str, str]:
    if result is None:
        return True, "", "", ""
    if isinstance(result, str):
        return True, _clean_text(result), "", ""
    getter = result.get if isinstance(result, dict) else lambda key, default=None: getattr(
        result, key, default)
    success = bool(getter("success", True))
    return (success, _clean_text(getter("stdout", "")),
            _clean_text(getter("stderr", "")), _clean_text(getter("reason", "")))


def _snapshot_scalars(snapshot: dict[str, Any]) -> list[tuple[str, Any]]:
    values = []
    for key, value in snapshot.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            values.append((str(key), value))
        if len(values) >= 16:
            break
    return values


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch tui",
        description="Interactive Textual UI for pi-batch agent sessions.",
    )
    parser.add_argument("--agent-bin", default="", help="agent CLI binary")
    parser.add_argument("--model", default="", help="model override")
    parser.add_argument("--provider", default="", help="provider override")
    parser.add_argument("--memory-mode", choices=("auto", "on", "off"), default="")
    parser.add_argument("--timeout", type=int, default=0, help="turn timeout in seconds")
    parser.add_argument("--cwd", default="", help="workspace directory")
    parser.add_argument("--session-name", default="tui", help="stable session display name")
    return parser


def _options_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> TuiOptions:
    if args.timeout < 0:
        parser.error("--timeout must be non-negative")
    cwd = ""
    if args.cwd:
        path = Path(args.cwd).expanduser().resolve()
        if not path.is_dir():
            parser.error(f"--cwd is not a directory: {path}")
        cwd = str(path)
    return TuiOptions(cwd=cwd, agent=args.agent_bin, model=args.model,
                      provider=args.provider, memory_mode=args.memory_mode,
                      timeout=args.timeout, session_name=args.session_name)


def _interactive_terminal() -> bool:
    capable = os.environ.get("TERM", "").lower() != "dumb"
    return bool(capable and getattr(sys.stdin, "isatty", lambda: False)()
                and getattr(sys.stdout, "isatty", lambda: False)())


def tui_main(
    argv: Optional[list[str]] = None,
    *,
    backend: Any = None,
    backend_factory: Optional[Callable[..., Any]] = None,
) -> int:
    """Parse TUI options and run interactively; non-TTY execution exits 2."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    options = _options_from_args(parser, args)
    if not _interactive_terminal():
        print("pi-batch tui requires interactive stdin and stdout terminals", file=sys.stderr)
        return 2
    app = PiBatchTUI(options, backend=backend, backend_factory=backend_factory)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(tui_main())
