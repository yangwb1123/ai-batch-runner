"""Interactive device authorization for private, short-lived Hub user sessions."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

from .hub_client import HubError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pi-batch fleet auth")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("login", "status", "logout"):
        action = actions.add_parser(name)
        action.add_argument("--config", default="",
                            help="private user JSON configuration (or PBATCH_HUB_USER_CONFIG)")
        if name == "login":
            action.add_argument("--timeout", type=float, default=5.0,
                                help="per-request HTTP timeout in seconds")
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except KeyboardInterrupt:
        print("Hub login cancelled.", file=sys.stderr)
        return 130
    except (HubError, ValueError, OSError) as exc:
        print(f"fleet auth: {exc}", file=sys.stderr)
        return 2


def _run(args) -> int:
    if args.action == "login" and (not math.isfinite(args.timeout) or not 0 < args.timeout <= 120):
        raise ValueError("HTTP timeout must be between 0 and 120 seconds")
    from .hub_user_auth_config import load_user_config
    from .hub_user_credentials import CredentialStore

    path = args.config or os.environ.get("PBATCH_HUB_USER_CONFIG", "").strip()
    if not path:
        raise ValueError("set --config or PBATCH_HUB_USER_CONFIG")
    config = load_user_config(path)
    store = CredentialStore(config)
    if args.action == "status":
        print(json.dumps(store.status(), sort_keys=True))
    elif args.action == "logout":
        store.logout()
        print("Local Hub session cleared. This does not revoke tokens at Snaplink.")
    else:
        _login(config, store, args.timeout)
    return 0


def _login(config, store, timeout: float) -> None:
    from .hub_device_oauth import DeviceAuthorization

    authorization = DeviceAuthorization(config, timeout=timeout)
    ticket = store.begin_login()
    try:
        challenge = authorization.start()
        print("Open: " + challenge["verification_uri"], flush=True)
        print("Enter code: " + challenge["user_code"], flush=True)
        print("Waiting for approval; press Ctrl-C to cancel.", flush=True)
        credential = authorization.poll(challenge)
        store.finish_login(ticket, credential)
    except BaseException:
        _cancel_pending(store, ticket)
        raise
    print("Hub user session saved. Set PBATCH_HUB_USER_CONFIG to this configuration file.")


def _cancel_pending(store, ticket: str) -> None:
    try:
        store.cancel_login(ticket)
    except (HubError, ValueError, OSError):
        print("Pending login could not be cleared; run fleet auth logout before retrying.",
              file=sys.stderr)
