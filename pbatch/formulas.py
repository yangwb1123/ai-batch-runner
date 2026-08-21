"""Deterministic AADM scoring formulas.

The formulas in ``docs/AADM.md`` are routing signals, not claims of truth.
This module keeps their arithmetic in one small, dependency-free place so
callers do not reimplement subtly different versions of the same gate.

Scores are normalized to ``[0, 1]``; criticality weights are non-negative
and may be greater than one. Invalid, non-finite values fail closed instead
of being silently clamped: a malformed score must not make a high-risk plan
look safe.
"""

from __future__ import annotations

import math
import argparse
import json
from collections.abc import Mapping, Sequence


def _unit(value: float, name: str) -> float:
    """Validate and return one normalized scalar."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number in [0, 1]")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be a finite number in [0, 1]")
    return number


def _weight(value: float, name: str) -> float:
    """Validate a non-negative weighting factor (not a normalized score)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number >= 0")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return number


def _items(value, name: str) -> list:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    return list(value)


def uncertainty_score(items: Sequence[Mapping] | None) -> float:
    """Calculate weighted uncertainty.

    ``U = sum(Criticality * (1 - Confidence)) / sum(Criticality)``.
    Empty input has no unresolved claims and returns ``0``; an item with zero
    criticality is accepted but contributes no weight.
    """
    rows = _items(items or [], "items")
    weighted = 0.0
    total = 0.0
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise ValueError(f"items[{index}] must be a mapping")
        criticality = _weight(item.get("criticality", 0.0),
                              f"items[{index}].criticality")
        confidence = _unit(item.get("confidence", 0.0),
                           f"items[{index}].confidence")
        weighted += criticality * (1.0 - confidence)
        total += criticality
    return round(weighted / total if total else 0.0, 4)


def weighted_uncertainty(items: Sequence[Mapping] | None) -> float:
    """Readable alias for :func:`uncertainty_score`."""
    return uncertainty_score(items)


def risk_item(severity: float, probability: float, blast_radius: float,
              irreversibility: float, detectability: float) -> float:
    """Calculate one risk item.

    ``Risk_i = Severity * Probability * BlastRadius * Irreversibility *
    (1 - Detectability)``.
    """
    values = {
        "severity": severity, "probability": probability,
        "blast_radius": blast_radius, "irreversibility": irreversibility,
        "detectability": detectability,
    }
    checked = {name: _unit(value, name) for name, value in values.items()}
    result = (checked["severity"] * checked["probability"]
              * checked["blast_radius"] * checked["irreversibility"]
              * (1.0 - checked["detectability"]))
    return round(result, 4)


def aggregate_risk(items: Sequence[Mapping | float] | None) -> float:
    """Aggregate risks without diluting the worst item.

    ``Risk = 0.7 * max(Risk_i) + 0.3 * WeightedMean(Risk_i)``.  Mapping rows
    may provide a precomputed ``risk`` and optional ``criticality`` weight;
    rows with the five risk dimensions are calculated through
    :func:`risk_item`.  Empty input returns ``0``.
    """
    rows = _items(items or [], "items")
    if not rows:
        return 0.0
    risks: list[tuple[float, float]] = []
    for index, item in enumerate(rows):
        if isinstance(item, Mapping):
            if "risk" in item:
                value = _unit(item["risk"], f"items[{index}].risk")
            else:
                dimensions = ("severity", "probability", "blast_radius",
                              "irreversibility", "detectability")
                if any(name not in item for name in dimensions):
                    raise ValueError(
                        f"items[{index}] needs risk or all risk dimensions")
                value = risk_item(*(item[name] for name in dimensions))
            weight = _weight(item.get("criticality", 1.0),
                             f"items[{index}].criticality")
        else:
            value = _unit(item, f"items[{index}]")
            weight = 1.0
        risks.append((value, weight))
    total_weight = sum(weight for _, weight in risks)
    weighted_mean = (sum(value * weight for value, weight in risks)
                     / total_weight if total_weight else 0.0)
    return round(0.7 * max(value for value, _ in risks)
                 + 0.3 * weighted_mean, 4)


def risk_score(items: Sequence[Mapping | float] | None) -> float:
    """Readable alias for :func:`aggregate_risk`."""
    return aggregate_risk(items)


def complexity_score(scope: float, coupling: float, state_complexity: float,
                     uncertainty: float, novelty: float,
                     change_surface: float) -> float:
    """Calculate the normalized AADM complexity score."""
    values = (scope, coupling, state_complexity, uncertainty, novelty,
              change_surface)
    checked = [_unit(value, name) for value, name in zip(
        values, ("scope", "coupling", "state_complexity", "uncertainty",
                 "novelty", "change_surface"))]
    weights = (0.20, 0.20, 0.15, 0.15, 0.15, 0.15)
    return round(sum(value * weight for value, weight in zip(checked, weights)),
                 4)


def confidence_score(evidence_coverage: float, prompt_clarity: float,
                     consistency: float, testability: float,
                     known_precedent: float) -> float:
    """Calculate current confidence from evidence, not model intuition."""
    values = (evidence_coverage, prompt_clarity, consistency, testability,
              known_precedent)
    checked = [_unit(value, name) for value, name in zip(
        values, ("evidence_coverage", "prompt_clarity", "consistency",
                 "testability", "known_precedent"))]
    weights = (0.35, 0.20, 0.15, 0.15, 0.15)
    return round(sum(value * weight for value, weight in zip(checked, weights)),
                 4)


def probe_value(impact_of_unknown: float,
                expected_uncertainty_reduction: float,
                probe_risk: float, probe_effort: float) -> float:
    """Calculate whether a low-cost probe is worth interrupting execution."""
    values = (impact_of_unknown, expected_uncertainty_reduction, probe_risk,
              probe_effort)
    checked = [_unit(value, name) for value, name in zip(
        values, ("impact_of_unknown", "expected_uncertainty_reduction",
                 "probe_risk", "probe_effort"))]
    return round(checked[0] * checked[1] - checked[2] - checked[3], 4)


__all__ = ["aggregate_risk", "complexity_score", "confidence_score",
           "probe_value", "risk_item", "risk_score", "uncertainty_score",
           "weighted_uncertainty"]


def formulas_main(argv: list | None = None) -> int:
    """`pi-batch formulas <metric>` — expose the deterministic score API."""
    parser = argparse.ArgumentParser(
        prog="pi-batch.py formulas",
        description="AADM deterministic uncertainty/risk/complexity/confidence formulas")
    sub = parser.add_subparsers(dest="metric", required=True)

    def items_parser(name: str, help_text: str):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--items", required=True,
                             help="JSON array of weighted claim/risk items")
        command.add_argument("--json", action="store_true")
        return command

    items_parser("uncertainty", "weighted uncertainty")
    items_parser("risk", "worst-case-preserving risk")
    complexity = sub.add_parser("complexity", help="normalized complexity")
    for name in ("scope", "coupling", "state-complexity", "uncertainty",
                 "novelty", "change-surface"):
        complexity.add_argument(f"--{name}", required=True, type=float)
    complexity.add_argument("--json", action="store_true")
    confidence = sub.add_parser("confidence", help="evidence confidence")
    for name in ("evidence-coverage", "prompt-clarity", "consistency",
                 "testability", "known-precedent"):
        confidence.add_argument(f"--{name}", required=True, type=float)
    confidence.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.metric in ("uncertainty", "risk"):
            items = json.loads(args.items)
            score = (uncertainty_score(items) if args.metric == "uncertainty"
                     else aggregate_risk(items))
        elif args.metric == "complexity":
            score = complexity_score(args.scope, args.coupling,
                                     args.state_complexity, args.uncertainty,
                                     args.novelty, args.change_surface)
        else:
            score = confidence_score(args.evidence_coverage,
                                     args.prompt_clarity, args.consistency,
                                     args.testability, args.known_precedent)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if getattr(args, "json", False):
        print(json.dumps({"metric": args.metric, "score": score},
                         ensure_ascii=False))
    else:
        print(f"{args.metric}: {score}")
    raise SystemExit(0)
