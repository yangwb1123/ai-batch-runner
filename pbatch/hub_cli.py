"""CLI commands for browsing and operating Agent sessions through the Hub."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any, Optional

from .hub_client import HubClient, HubError
from .hub_gateway import HubGateway, HubGatewayConfig


def hub_main(argv: Optional[list[str]] = None) -> int:
    """Run ``pi-batch fleet`` actions."""
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == "serve":
        return _serve_main(values[1:])
    if values and values[0] == "auth":
        from .hub_auth_cli import main as auth_main
        return auth_main(values[1:])
    if values and values[0] == "tasks":
        from .hub_task_center_cli import main as tasks_main
        return tasks_main(values[1:])
    if values and values[0] == "compute":
        from .hub_compute_cli import main as compute_main
        return compute_main(values[1:])
    if values and values[0] == "integrations":
        from .hub_integration_cli import main as integration_main
        return integration_main(values[1:])
    parser = _parser()
    args = parser.parse_args(values)
    try:
        return _run_action(args)
    except (HubError, ValueError, RuntimeError, OSError) as exc:
        print(f"fleet: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-batch fleet",
                                     description="Remote Agent sessions and Hub Gateway")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("instances", "sessions", "show", "send", "events", "cancel",
                 "agent", "tui", "init", "serve", "compute", "integrations", "auth",
                 "session-create", "session-request", "tasks"):
        _add_action(actions, name)
    _common_hub(actions.choices["instances"])
    _common_hub(actions.choices["sessions"])
    _common_hub(actions.choices["show"])
    _common_hub(actions.choices["send"])
    _common_hub(actions.choices["events"])
    _common_hub(actions.choices["cancel"])
    _common_hub(actions.choices["tui"])
    for name in ("session-create", "session-request"):
        _common_hub(actions.choices[name])
        actions.choices[name].add_argument("--json", action="store_true")
    _configure_agent(actions.choices["agent"])
    _configure_init(actions.choices["init"])
    _configure_queries(actions)
    _configure_close(actions)
    _configure_operations(actions)
    return parser


def _configure_close(actions):
    for name in ("session-close", "session-close-request"):
        parser = actions.add_parser(name, help="close an idle remote session" if name ==
                                    "session-close" else "inspect a remote session close request")
        _common_hub(parser)
        parser.add_argument("--json", action="store_true")
        if name == "session-close":
            parser.add_argument("session_id")
            parser.add_argument("--idempotency-key", default="")
        else:
            parser.add_argument("request_id")

def _configure_operations(actions):
    parser = actions.add_parser("session-operations", help="browse accepted session requests")
    _common_hub(parser)
    parser.add_argument("--instance", default="")
    parser.add_argument("--before", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")


def _add_action(actions, name: str) -> argparse.ArgumentParser:
    return actions.add_parser(name, help=_help_for(name))


def _help_for(name: str) -> str:
    return {
        "auth": "sign in, inspect, or clear a private Hub user session",
        "integrations": "inspect lifecycle deliveries and retry failures",
        "compute": "discover devices and dispatch computations",
        "instances": "list connected pi-batch instances",
        "sessions": "list sessions across instances",
        "tasks": "read authenticated Hub task/attention facts; no execution controls",
        "session-create": "request a fresh session on one remote instance",
        "session-request": "inspect a remote session creation request",
        "show": "show one remote session",
        "send": "send a prompt to one exact session",
        "events": "read or follow session events",
        "cancel": "cancel a remote turn",
        "agent": "run an outbound session Gateway",
        "tui": "open the remote session TUI",
        "init": "create a local development Hub configuration",
        "serve": "start the Agent Hub HTTP service",
    }[name]


def _common_hub(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hub", default="", help="Hub origin (or PBATCH_HUB_URL)")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="HTTP timeout in seconds (default: 5)")
    if parser.prog.endswith(" sessions"):
        parser.add_argument("--instance-id", default="")
    elif parser.prog.endswith(" show"):
        parser.add_argument("session_id")
    elif parser.prog.endswith(" send"):
        parser.add_argument("session_id")
        parser.add_argument("prompt", nargs="+")
        parser.add_argument("--idempotency-key", default="",
                            help="stable retry key; generated if omitted")
    elif parser.prog.endswith(" events"):
        parser.add_argument("session_id")
        parser.add_argument("--after", default="", help="resume after this cursor")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--follow", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=1.0)
    elif parser.prog.endswith(" cancel"):
        parser.add_argument("turn_id")
    elif parser.prog.endswith(" session-create"):
        parser.add_argument("instance_id")
        parser.add_argument("--name", required=True)
        parser.add_argument("--idempotency-key", default="")
    elif parser.prog.endswith(" session-request"):
        parser.add_argument("request_id")


def _configure_agent(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hub", default="", help="Hub origin (or PBATCH_HUB_URL)")
    parser.add_argument("--oauth-config", default="",
                        help="private machine OAuth JSON (or PBATCH_HUB_OAUTH_CONFIG)")
    parser.add_argument("--cwd", default="", help="local project directory")
    parser.add_argument("--state-dir", default="", help="private Gateway SQLite directory")
    parser.add_argument("--instance-id", default="", help="stable instance id (generated once if absent)")
    parser.add_argument("--name", default="")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--agent", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--runtime", default="auto",
                        choices=("auto", "pi", "pi-rpc", "codex", "codex-exec", "legacy"))
    parser.add_argument("--sessions", type=int, default=1,
                        help="initial sessions (0 through --max-sessions)")
    parser.add_argument("--max-sessions", type=int, default=8,
                        help="total initial and requested sessions (1 through 8)")
    parser.add_argument("--compute-tools", action="store_true",
                        help="provide active-turn compute tools to the local Agent")
    parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="Hub HTTP timeout in seconds")


def _configure_init(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--directory", required=True,
                        help="new directory for development config and tokens")
    parser.add_argument("--instances", nargs="+", default=["alpha", "beta"],
                        help="Gateway instance labels (default: alpha beta)")


def _configure_queries(actions) -> None:
    actions.choices["instances"].add_argument("--json", action="store_true")
    actions.choices["sessions"].add_argument("--json", action="store_true")
    actions.choices["show"].add_argument("--json", action="store_true")


def _run_action(args) -> int:
    if args.action == "init":
        from .hub_setup import create_development_config
        result = create_development_config(args.directory,
                                          instances=tuple(args.instances))
        return _print_object(_init_paths(result), True)
    if args.action == "agent":
        return _run_gateway(args)
    client = HubClient.from_env(args.hub, timeout=args.timeout)
    if args.action == "session-operations":
        return _session_operations(client, args)
    if args.action in {"session-close", "session-close-request"}:
        return _session_close(client, args)
    if args.action in {"session-create", "session-request"}:
        try:
            return _session_creation(client, args)
        except HubError as exc:
            from .hub_auth_feedback import error_status
            print(error_status("Session request failed; retain its key to retry", exc), file=sys.stderr)
            return 2
    if args.action == "instances":
        return _print_records(client.list_instances(), args.json, "instance")
    if args.action == "sessions":
        rows = client.list_sessions(args.instance_id)
        return _print_records(rows, args.json, "session")
    if args.action == "show":
        return _print_object(client.get_session(args.session_id), args.json)
    if args.action == "send":
        key = args.idempotency_key or uuid.uuid4().hex
        print(f"Idempotency-Key: {key}", file=sys.stderr)
        result = client.send_prompt(args.session_id, " ".join(args.prompt),
                                    key)
        return _print_object(result, True)
    if args.action == "events":
        return _read_events(client, args)
    if args.action == "cancel":
        return _print_object(client.cancel_turn(args.turn_id), True)
    if args.action == "tui":
        from .hub_tui import run_hub_tui
        return run_hub_tui(client)
    raise ValueError(f"unsupported fleet action: {args.action}")


def _session_creation(client: HubClient, args) -> int:
    if args.action == "session-request":
        return _print_object(client.session_request(args.request_id), args.json)
    from .hub_session_client import creation_key, session_name
    name = session_name(args.name)
    key = creation_key(args.idempotency_key or uuid.uuid4().hex)
    print(f"Idempotency-Key: {key}", file=sys.stderr, flush=True)
    result = client.create_session(args.instance_id, name, idempotency_key=key)
    return _print_object(result, args.json)


def _session_close(client: HubClient, args) -> int:
    from .hub_session_close_client import close_error, close_key, close_target
    try:
        if args.action == "session-close-request":
            return _print_object(client.session_close_request(args.request_id), args.json)
        session_id = close_target(args.session_id)
        key = close_key(args.idempotency_key or uuid.uuid4().hex)
        print(f"Idempotency-Key: {key}", file=sys.stderr, flush=True)
        result = client.close_session(session_id, idempotency_key=key)
        return _print_object(result, args.json)
    except HubError as exc:
        print(close_error("Close request unconfirmed; retain its key to recover", exc), file=sys.stderr)
        return 2

def _session_operations(client, args):
    from .hub_session_operations_client import history_error, visible
    try:
        result = client.list_session_operations(args.instance, before=args.before, limit=args.limit)
    except HubError as exc:
        print(history_error(exc), file=sys.stderr)
        return 2
    if args.json:
        return _print_object(result, True)
    for row in result["data"]:
        print(visible(f"{row['operation']} {row['status']} request={row['request_id']} "
                      f"instance={row['instance_id']} session={row['session_id'] or '-'} name={row['name']}"))
    if not result["data"]:
        print("No accepted session requests.")
    print("next_cursor: " + result["next_cursor"])
    return 0


def _run_gateway(args) -> int:
    if os.environ.get("PBATCH_HUB_USER_CONFIG", "").strip():
        raise ValueError("Gateway requires machine credentials; unset PBATCH_HUB_USER_CONFIG")
    options = HubGatewayConfig(
        hub_url=args.hub, cwd=args.cwd, state_dir=args.state_dir,
        instance_id=args.instance_id, name=args.name, project_id=args.project_id,
        agent=args.agent, model=args.model, provider=args.provider,
        runtime=args.runtime, sessions=args.sessions, max_sessions=args.max_sessions,
        compute_tools=args.compute_tools,
        heartbeat_interval=args.heartbeat_interval,
        poll_interval=args.poll_interval)
    gateway = HubGateway(options, client=HubClient.from_env(
        args.hub, timeout=min(args.timeout, 10.0), oauth_config=args.oauth_config))
    print(f"Gateway {gateway.instance_id} registered (generation {gateway.generation})")
    try:
        gateway.run_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        gateway.close()
    return 0


def _read_events(client: HubClient, args) -> int:
    cursor = args.after
    try:
        while True:
            events = client.session_events(args.session_id, after=cursor,
                                           limit=args.limit)
            for event in events:
                _print_event(event)
                cursor = _event_cursor(event) or cursor
            if not args.follow:
                return 0
            time.sleep(max(0.1, min(args.poll_interval, 30.0)))
    except KeyboardInterrupt:
        return 130


def _print_records(rows: list[dict[str, Any]], as_json: bool, label: str) -> int:
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"No {label}s found.")
        return 0
    for row in rows:
        print("  ".join(_display(row, key) for key in _record_fields(label)))
    return 0


def _record_fields(label: str) -> tuple[str, ...]:
    if label == "instance":
        return ("instance_id", "name", "project_id", "online", "last_seen")
    return ("session_id", "instance_id", "name", "status", "controllable")


def _print_object(value: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        for key, item in value.items():
            print(f"{key}: {item}")
    return 0


def _print_event(event: dict[str, Any]) -> None:
    kind = str(event.get("kind", "event"))
    payload = event.get("payload", {})
    text = payload.get("text", "") if isinstance(payload, dict) else ""
    detail = text or (payload.get("reason", "") if isinstance(payload, dict) else "")
    print(f"{event.get('cursor', '')} {kind} {detail}".rstrip())


def _event_cursor(event: dict[str, Any]) -> str:
    return str(event.get("cursor", "") or "")


def _display(record: dict[str, Any], key: str) -> str:
    value = record.get(key, "")
    return f"{key}={value}" if value != "" else ""


def _serve_main(argv: list[str]) -> int:
    from .hub_http import hub_serve_main

    return hub_serve_main(argv)


def _init_paths(result: dict[str, Any]) -> dict[str, Any]:
    """Expose config locations only; development bearer tokens stay private."""
    config = result.get("config", "")
    user_token = result.get("user_token_file", "")
    gateway_tokens = result.get("gateway_token_files", {})
    return {"config": str(config), "user_token_file": str(user_token),
            "gateway_token_files": {
                str(name): str(path) for name, path in gateway_tokens.items()
            } if isinstance(gateway_tokens, dict) else {}}


def main(argv: Optional[list[str]] = None) -> int:
    return hub_main(argv)


__all__ = ["hub_main", "main"]
