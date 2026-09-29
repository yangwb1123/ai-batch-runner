"""Command-line options for the interactive TUI shell."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import threading

from .tui_shell import TuiOptions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch tui",
        description="Interactive Textual UI for pi-batch agent sessions.",
    )
    parser.add_argument("--agent-bin", default="", help="agent CLI binary")
    parser.add_argument("--model", default="", help="model override")
    parser.add_argument("--provider", default="", help="provider override")
    parser.add_argument(
        "--runtime", choices=(
            "auto", "pi-rpc", "app-server", "structured", "legacy"),
        default="auto", help="TUI runtime selection")
    parser.add_argument("--memory-mode", choices=("auto", "on", "off"), default="")
    parser.add_argument("--timeout", type=int, default=0, help="turn timeout in seconds")
    parser.add_argument("--startup-timeout", type=float, default=None,
                        help="App Server initialization timeout in seconds (default: 15)")
    parser.add_argument("--cwd", default="", help="workspace directory")
    parser.add_argument("--session-name", default="tui", help="stable session display name")
    parser.add_argument("--pipeline", default="",
                        help="start and observe a workspace-relative Pipeline YAML")
    parser.add_argument("--orchestrator-only", action="store_true",
                        help="disable Pi tools and return pipeline results to this session for review")
    parser.add_argument("--persistent-pipelines", action="store_true",
                        help="run pipelines in a detached, recoverable worker (requires --orchestrator-only)")
    parser.add_argument("--native-session", default="",
                        help="recover an exact read-only native Pipeline journal")
    return parser


def options_from_args(parser: argparse.ArgumentParser,
                      args: argparse.Namespace) -> TuiOptions:
    if args.timeout < 0:
        parser.error("--timeout must be non-negative")
    if args.startup_timeout is not None:
        if (not math.isfinite(args.startup_timeout)
                or not 0 < args.startup_timeout <= threading.TIMEOUT_MAX):
            parser.error(
                "--startup-timeout must be finite, positive and within the platform limit")
        if args.runtime != "app-server":
            parser.error("--startup-timeout requires --runtime app-server")
    cwd = ""
    if args.cwd:
        path = Path(args.cwd).expanduser().resolve()
        if not path.is_dir():
            parser.error(f"--cwd is not a directory: {path}")
        cwd = str(path)
    if args.native_session and args.pipeline:
        parser.error("--native-session cannot be combined with --pipeline")
    if args.orchestrator_only and args.runtime not in {"auto", "pi-rpc"}:
        parser.error("--orchestrator-only requires --runtime auto or pi-rpc")
    if args.orchestrator_only and args.native_session:
        parser.error("--orchestrator-only cannot be used with --native-session")
    if args.persistent_pipelines and not args.orchestrator_only:
        parser.error("--persistent-pipelines requires --orchestrator-only")
    return TuiOptions(
        cwd=cwd, agent=args.agent_bin, model=args.model, provider=args.provider,
        memory_mode=args.memory_mode, timeout=args.timeout,
        session_name=args.session_name, startup_timeout=args.startup_timeout,
        runtime=args.runtime, pipeline=args.pipeline,
        orchestrator_only=args.orchestrator_only,
        persistent_pipelines=args.persistent_pipelines,
        native_session=args.native_session)


# Backwards-compatible private aliases used by the TUI entrypoint/tests.
_build_parser = build_parser
_options_from_args = options_from_args
