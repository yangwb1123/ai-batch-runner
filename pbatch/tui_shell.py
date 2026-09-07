"""Static shell copy and command parsing for the product TUI."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Optional

from .tui_command_specs import render_slash_help

SLASH_HELP = render_slash_help() + """

Type a command prefix and press Tab or Right to complete it. Enter completes a
partial name first; exact names submit normally. Command-name completion is
local and never contacts the runtime or executes a command.

Typing `/model ` or `/thinking ` in managed Pi RPC loads one bounded,
read-only active-session snapshot and opens a local fuzzy picker; pasted text
stays inert until Tab. In that picker, Tab or Enter only inserts the selected
value. Press Enter again in the composer to submit through fresh validation.

Other model queries open the fuzzy picker. In managed Pi RPC, Ctrl+S only
reports that persistence is unavailable and leaves the picker open; pickers for
older Pi or global settings scope are read-only.

Text without a leading `/` is sent to the agent.
"""

WELCOME = """\
# pi-batch

Describe the work you want to run. Use `/help` for commands or **Ctrl+P** for
the command palette. Type a slash-command prefix and press **Tab** to complete.
"""


@dataclass
class TuiOptions:
    """Backend options supplied by ``tui_main`` and slash commands."""

    cwd: str = ""
    agent: str = ""
    model: str = ""
    provider: str = ""
    memory_mode: str = ""
    timeout: int = 0
    session_name: str = "tui"
    runtime: str = "auto"
    pipeline: str = ""
    native_session: str = ""

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


def parse_slash_command(text: str) -> Optional[SlashInvocation]:
    """Parse a slash command without treating malformed input as a prompt."""

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
