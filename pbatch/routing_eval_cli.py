"""Command-line interface and human-readable rendering for routing reports."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import Optional

from .agent_ledger import QUERY_MAX_RECORDS
from .routing_eval import routing_report

_MAX_LIMIT = QUERY_MAX_RECORDS


def _since_value(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("since must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("since timestamp must include a timezone")
    return parsed.isoformat()


def _positive_limit(value: str) -> int:
    number = int(value)
    if number < 1 or number > _MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"limit must be 1..{_MAX_LIMIT}")
    return number


def _validate_pair_args(parser: argparse.ArgumentParser, args) -> None:
    paired = bool(args.baseline_model)
    if bool(args.baseline_model) != bool(args.candidate_model):
        parser.error("--baseline-model and --candidate-model must be used together")
    if bool(args.baseline_provider) != bool(args.candidate_provider):
        parser.error("--baseline-provider and --candidate-provider must be used together")
    if (args.baseline_provider or args.candidate_provider) and not paired:
        parser.error("provider selectors require a paired model comparison")
    same_route = (args.baseline_model == args.candidate_model
                  and args.baseline_provider == args.candidate_provider)
    if paired and same_route:
        parser.error("baseline and candidate routes must differ")
    if args.provider and paired:
        parser.error("use --baseline-provider/--candidate-provider for paired runs")
    if args.model and paired:
        parser.error("--model cannot filter a paired baseline comparison")


def routing_eval_main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pi-batch routing-eval",
        description="Compare observed routing cost, latency, outcomes, and retries")
    parser.add_argument("--limit", type=_positive_limit, default=_MAX_LIMIT)
    parser.add_argument("--since", type=_since_value, default=None,
                        help="include ledger observations at or after ISO-8601 time")
    parser.add_argument("--agent", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--tier", default="")
    parser.add_argument("--requested-tier", default="")
    parser.add_argument("--baseline-model", default="")
    parser.add_argument("--candidate-model", default="")
    parser.add_argument("--baseline-provider", default="")
    parser.add_argument("--candidate-provider", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    _validate_pair_args(parser, args)
    report = routing_report(
        limit=args.limit, agent=args.agent, model=args.model,
        provider=args.provider, role=args.role, source=args.source,
        tier=args.tier, requested_tier=args.requested_tier,
        since_ts=args.since or "", baseline_model=args.baseline_model,
        candidate_model=args.candidate_model,
        baseline_provider=args.baseline_provider,
        candidate_provider=args.candidate_provider)
    if report["error"]:
        print(f"routing-eval: cannot read ledger: {report['error']}")
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0


def _print_report(report: dict) -> None:
    print("# Routing evaluation (observed only)")
    print(f"records={report['records_read']} skipped={report['records_skipped']} "
          f"truncated={str(report['truncated']).lower()}")
    if not report["routes"]:
        print("No attributed execution records. Enable the agent ledger and run tasks.")
        return
    for route in report["routes"]:
        print("\n## {model} / {provider} / {agent} / {role} "
              "({route_source}:{route_requested_tier}->{route_tier}, "
              "downgraded={route_downgraded})".format(**route))
        print(f"tasks={route['tasks']} success_rate={_display(route['success_rate'])} "
              f"rework_rate={_display(route['rework_rate'])} "
              f"calls={route['calls']}")
        print(f"mean_latency_ms={_display(route['mean_latency_ms'])} "
              f"validator_pass_rate={_display(route['validator_pass_rate'])} "
              f"cost_total={_display(route['cost_total'])} "
              f"cost_coverage={_display(route['cost_coverage'])} "
              f"tokens_total={_display(route['tokens_total'])}")
    comparison = report.get("comparison")
    if comparison:
        print(f"\nPaired comparison: {comparison['candidate_model']}"
              f"[{comparison['candidate_provider']}] vs "
              f"{comparison['baseline_model']}[{comparison['baseline_provider']}] "
              f"({comparison['paired_fingerprints']} task fingerprints)")
        for name, values in comparison["metrics"].items():
            print(f"{name}: candidate-baseline="
                  f"{_display(values['candidate_minus_baseline'])} "
                  f"(paired={values['paired_fingerprints']})")


def _display(value: object) -> str:
    return "unknown" if value is None else str(value)
