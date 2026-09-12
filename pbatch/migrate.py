"""CLI/report orchestration for source-first migration rehearsal.

Manifest validation and deterministic evidence calculations live in
``migrate_spec.py`` so this command module stays below the repository
file-size budget.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

from .config import log
from .migrate_spec import (
    DEFAULT_EXCLUDES,
    SCHEMA_VERSION,
    STATUS_WEIGHT,
    STATUSES,
    MigrationSpecError,
    _assess_capability,
    _inventory,
    _metrics,
    _plan,
    _root,
    _run_gate,
    build_report,
    load_manifest,
)


def _common(parser: argparse.ArgumentParser, baseline: bool = False) -> None:
    parser.add_argument("--source" if not baseline else "--baseline", required=True,
                        help="source/reference project" if not baseline else "baseline project")
    parser.add_argument("--target", required=True, help="target/current project")
    parser.add_argument("--manifest", default="", help="migration/v1 YAML contract")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="", help="explicit JSON report path")
    parser.add_argument("--run-gates", action="store_true", help="run declared acceptance commands; never edit code")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--budget-minutes", type=float, default=None)
    parser.add_argument("--strict", action="store_true", help="exit 1 when the contract is not fully verified")


def _write_report(path: str, report: dict) -> None:
    if not path:
        return
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)


def _print_report(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    metrics = report["metrics"]
    print(f"# Migration {report['mode']}: {report['manifest']}")
    print(f"source: {report['source']}\ntarget: {report['target']}")
    print("capabilities: {capabilities} | verified: {verified} | missing: {missing} | "
          "unverified: {unverified}".format(
              capabilities=metrics["capabilities"], verified=metrics["counts"].get("verified", 0),
              missing=metrics["counts"].get("missing", 0),
              unverified=metrics["counts"].get("present_unverified", 0)))
    print(f"weighted completion: {metrics['weighted_completion']:.1%} | ready: {metrics['ready']}")
    print(f"plan: {report['plan']['pending']} pending / {report['plan']['waves']} waves")
    for item in report["plan"]["items"]:
        print(f"- W{item['wave']} {item['priority']} {item['id']} [{item['status']}] → {item['route']}")


def migrate_main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-batch.py migrate", description="Source-first compare/plan/rehearse/upgrade")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("compare", "plan", "rehearse"):
        child = sub.add_parser(name, help=f"{name} without code mutation")
        _common(child)
    upgrade = sub.add_parser("upgrade", help="compare a baseline project with its current target")
    _common(upgrade, baseline=True)
    args = parser.parse_args(argv)
    if args.concurrency < 1 or (args.budget_minutes is not None and args.budget_minutes < 0):
        parser.error("--concurrency must be positive and --budget-minutes must be non-negative")
    try:
        source = args.source if args.command != "upgrade" else args.baseline
        report = build_report(source, args.target, args.manifest, "upgrade" if args.command == "upgrade" else args.command,
                              args.run_gates, args.concurrency, args.budget_minutes)
        if args.command == "rehearse" and args.budget_minutes is not None and report["rehearsal"]["budget_status"] == "exceeded":
            report["rehearsal"]["ready"] = False
        _write_report(args.out, report)
        _print_report(report, args.json)
    except (MigrationSpecError, OSError, ValueError) as exc:
        log.error("migrate: %s", exc)
        if args.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "invalid", "error": str(exc)}, ensure_ascii=False))
        return 2
    if args.strict and not report["metrics"]["ready"]:
        return 1
    if args.command == "rehearse" and report["rehearsal"].get("budget_status") == "exceeded":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(migrate_main())
