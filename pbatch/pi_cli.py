"""Canonical Pi CLI passthrough.

This module deliberately does not parse Pi arguments.  Pi remains the sole
owner of its interactive, print, JSON, RPC, authentication, package, and
export protocols; pi-batch only selects a safe executable and forwards argv.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

from . import config
from .pi_version import select_pi_executable_token


_ATTRIBUTION_ENV = "PBATCH_PI_LAUNCHER"


def _configured_pi() -> str:
    """Choose an explicit Pi binary without reusing another agent adapter."""
    configured = str(config.AGENT_BIN).strip()
    basename = Path(configured).name.lower()
    fallback = configured if basename in {"pi", "pi.exe"} else "pi"
    return select_pi_executable_token(fallback=fallback)


def _passthrough_args(argv: Sequence[str] | None) -> list[str]:
    return list(sys.argv[1:] if argv is None else argv)


def _child_env() -> dict[str, str]:
    """Copy the caller environment and add namespaced attribution only."""
    env = os.environ.copy()
    env.setdefault(_ATTRIBUTION_ENV, "pi-batch")
    return env


def _run_pi(command: Sequence[str], env: Mapping[str, str]) -> int:
    """Invoke Pi without a shell while inheriting cwd and standard streams."""
    completed = subprocess.run(list(command), env=dict(env), check=False)
    return completed.returncode

def _exit_code(returncode: int) -> int:
    return 128 + abs(returncode) if returncode < 0 else returncode


def pi_main(argv: Sequence[str] | None = None) -> int:
    """Run the canonical Pi CLI with its argv and process semantics intact."""
    binary = _configured_pi()
    command = [binary, *_passthrough_args(argv)]
    try:
        return _exit_code(_run_pi(command, _child_env()))
    except FileNotFoundError:
        print(
            f"pi-batch pi: Pi executable not found: {binary!r}; "
            "install @earendil-works/pi-coding-agent or set PBATCH_PI_BIN",
            file=sys.stderr,
        )
        return 2
    except PermissionError:
        print(f"pi-batch pi: Pi executable is not runnable: {binary!r}",
              file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


__all__ = ["pi_main"]
