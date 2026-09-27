"""Use the embedded interpreter for explicit bundled validator commands."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Optional

from .launcher import SCRIPT_FLAG, is_frozen, script_argv

# Match only a leading Python invocation. Preserve the shell suffix verbatim;
# never reparse/requote operators, substitutions or project-owned commands.
_WORD = r'''(?:[^\s'";&|<>`$()]|'[^']*'|"[^"$`]*")+'''
_PYTHON_SCRIPT = re.compile(
    rf"^\s*python(?:3)?\s+(?P<script>{_WORD})(?=\s|$)")
# Accept only literal words emitted by shlex.join/quote. Shell operators,
# expansion, globs, comments and line continuations retain shell execution.
_LITERAL_WORD = r'''(?:[\w@%+=:,./-]|'[^'\r\n]*'|"[^"$`\\\r\n]*")+'''
_LITERAL_COMMAND = re.compile(rf"[ \t]*{_LITERAL_WORD}(?:[ \t]+{_LITERAL_WORD})*[ \t]*")


def embedded_checker_command(command: str) -> str:
    """Replace only a literal, trusted bundled script; leave project tools alone."""
    if not is_frozen():
        return command
    match = _PYTHON_SCRIPT.match(command)
    if match is None:
        return command
    try:
        script, = shlex.split(match.group("script"))
        if not Path(script).is_absolute():
            return command
        invocation = script_argv(script)
    except ValueError:
        return command
    return shlex.join(invocation) + command[match.end():]


def embedded_checker_argv(command: str) -> Optional[list[str]]:
    """Decode a literal embedded invocation without passing POSIX quoting to cmd.exe.

    Only this executable's private checker entrypoint qualifies. Project shell
    commands, including compound checker commands, retain their shell semantics.
    """
    if not is_frozen() or _LITERAL_COMMAND.fullmatch(command) is None:
        return None
    try:
        argv = shlex.split(command)
        if len(argv) < 3 or argv[:2] != [sys.executable, SCRIPT_FLAG]:
            return None
        return argv if argv[:3] == script_argv(argv[2]) else None
    except ValueError:
        return None
