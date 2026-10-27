"""Read-only inspection of artifact validator invocation receipts."""

import argparse
import json
import sys

from .validation_receipts import read_receipts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="abr validation", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    command = commands.add_parser("receipts", help="inspect recent validator receipts")
    command.add_argument("--limit", type=int, default=50, metavar="N",
                         help="maximum receipts to show (1..200; default: 50)")
    command.add_argument("--json", action="store_true", help="print a JSON object")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 200:
        print("validation receipts: --limit must be 1..200", file=sys.stderr)
        return 2
    records = read_receipts(limit=args.limit)
    if args.json:
        print(json.dumps({"receipts": records}, ensure_ascii=False, indent=2))
    else:
        print(f"validation receipts: {len(records)} record(s) (newest last)")
        for record in records:
            print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
