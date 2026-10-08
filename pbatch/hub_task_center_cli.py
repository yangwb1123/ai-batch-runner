"""Authenticated Hub-native tasks, not local history discovery or remote task controls."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

from .hub_client import HubClient, HubError
from .hub_task_center_api import options
from .task_center_cli import format_attention, format_tasks
from .task_center_terminal_view import validate_options


def _parser():
    parser = argparse.ArgumentParser(prog="pi-batch fleet tasks", description=__doc__)
    parser.add_argument("--hub", default="", help="Hub origin (or PBATCH_HUB_URL)")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--source", default="", help="complete source ID from this authorized Hub window")
    parser.add_argument("--compute", action="store_true", help="explicit separate compute metadata view; needs agent.tasks:read, never merges native/local tasks")
    parser.add_argument("--source-sessions", action="store_true", help="explicit compute source-session identity references; also needs agent.sessions:read, no execution parent or billing equivalence")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--id", default="", help="complete task ID in this window, not a session command target")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--follow", action="store_true", help="poll metadata; JSON follow is JSONL")
    parser.add_argument("--attention", action="store_true", help="observed facts only, no intervention authority")
    parser.add_argument("--tui", action="store_true", help="independent read-only remote task viewer")
    return parser


def _validate(args):
    validate_options(args.limit, args.interval, args.id)
    options(args.limit, args.source)
    if args.source_sessions and not args.compute:
        raise ValueError("--source-sessions requires --compute; no native/local fallback")
    if args.tui and (args.json or args.follow or args.attention):
        raise ValueError("--tui cannot be combined with --json, --follow or --attention")
    if args.attention and args.id:
        raise ValueError("--attention cannot be combined with --id")


def _print_view(args, value):
    if args.attention:
        value = value["attention"]
    elif args.id:
        value = next((row for row in value["tasks"] if row["task_id"] == args.id), None)
        if value is None:
            raise ValueError("task not in the authorized window; not proof of completion, deletion or authority")
    if args.json or args.id:
        print(json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    else:
        output = format_attention(value) if args.attention else format_tasks(value)
        if args.source_sessions and not args.attention:
            from .hub_task_source_sessions import format_references
            output += "\n" + format_references(value)
        print(re.sub(r"[\x7f-\x9f\u202a-\u202e\u2066-\u2069]", " ", output))


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        _validate(args)  # before credential reads, authentication, transport or TUI creation
        client = HubClient.from_env(args.hub, timeout=args.timeout)
        if args.tui:
            from .hub_task_center_terminal import run_terminal
            return run_terminal(client, args.limit, args.source, args.id, args.interval,
                                compute=args.compute, source_sessions=args.source_sessions)
        read = client.compute_source_session_snapshot if args.source_sessions else (
            client.compute_task_snapshot if args.compute else client.task_snapshot)
        while True:
            _print_view(args, read(args.limit, args.source))
            if not args.follow:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0  # observer exit, never task success/cancellation
    except HubError as exc:
        print(f"fleet tasks: metadata unavailable (HTTP {exc.status}; no cached facts retained)", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(f"fleet tasks: {exc}", file=sys.stderr)
        return 2
