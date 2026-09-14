"""Inspect or retry lifecycle deliveries through the authenticated Hub API."""

import argparse
import json
import sys
from urllib.parse import quote, urlencode

from .hub_client import HubClient, HubError


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pi-batch fleet integrations", description=__doc__)
    parser.add_argument("--hub", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("deliveries")
    listing.add_argument("--state", choices=["pending", "sending", "retrying", "failed", "delivered"])
    listing.add_argument("--after", type=int, default=0)
    listing.add_argument("--limit", type=int, default=100)
    retry = sub.add_parser("retry")
    retry.add_argument("delivery_id")
    args = parser.parse_args(argv)
    try:
        client = HubClient.from_env(args.hub)
        if args.command == "deliveries":
            if not 1 <= args.limit <= 200:
                raise ValueError("limit must be between 1 and 200")
            params = {"after": args.after, "limit": args.limit}
            if args.state:
                params["state"] = args.state
            path = "/integrations/deliveries?" + urlencode(params)
            rows, cursor = client._request("GET", path, include_cursor=True)
            result = {"data": rows, "next_cursor": cursor}
        else:
            path = "/integrations/deliveries/" + quote(args.delivery_id, safe="") + "/retry"
            result = client._request("POST", path, {}, accepted=(202,))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (HubError, OSError, ValueError) as exc:
        print(f"integration request failed: {exc}", file=sys.stderr)
        return 2
