"""Scorecard rendering and the ``campaign-eval`` CLI.

Kept apart from the scoring core so the core stays small enough to read in one
sitting: ``campaign_evaluation`` decides, this module reports.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .campaign_evaluation import (BREACH, DIMENSIONS, OK, Scorecard, _RANK,
                                  compare, scorecard_from_dict)


def render_markdown(card: Scorecard) -> str:
    """Human-readable scorecard: dimensions, metrics, then flags by severity."""
    lines = [f"# Campaign evaluation ({card.status.upper()})", "",
             f"- generated: `{card.generated_at}`",
             f"- window: {card.window_hours:.1f} h"]
    for dimension in DIMENSIONS:
        metrics = card.dimensions.get(dimension)
        if not metrics:
            continue
        worst = max((flag.status for flag in card.flags
                     if flag.dimension == dimension), key=lambda s: _RANK[s],
                    default=OK)
        lines += ["", f"## {dimension} ({worst})", ""]
        for key, value in metrics.items():
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False)
            elif value is None:
                rendered = "unavailable"
            else:
                rendered = str(value)
            lines.append(f"- `{key}`: {rendered}")
    if card.flags:
        lines += ["", "## flags", ""]
        for flag in sorted(card.flags, key=lambda f: (-_RANK[f.status],
                                                      f.dimension, f.name)):
            threshold = ("" if flag.threshold is None
                         else f" (threshold {flag.threshold:g})")
            lines.append(f"- **{flag.status}** `{flag.dimension}/{flag.name}`: "
                         f"{flag.detail}{threshold}")
    return "\n".join(lines) + "\n"


def render_trend(trend: dict) -> str:
    lines = ["# Scorecard trend", "",
             f"- status: {trend['status']['from']} -> {trend['status']['to']}"
             f" (regressed: {trend['status']['regressed']})", "",
             "| metric | before | after | delta |",
             "| --- | --- | --- | --- |"]
    for name, values in trend.items():
        if name in ("status", "new_flags", "cleared_flags"):
            continue
        lines.append(f"| `{name}` | {values['before']} | {values['after']} | "
                     f"{values['delta']} |")
    if trend["new_flags"]:
        lines += ["", f"- new flags: {', '.join(trend['new_flags'])}"]
    if trend["cleared_flags"]:
        lines += [f"- cleared flags: {', '.join(trend['cleared_flags'])}"]
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="campaign-eval",
        description="Score a repository campaign window across every dimension")
    parser.add_argument("--state", required=True, help="campaign state.jsonl")
    parser.add_argument("--repo", required=True, help="campaign repository root")
    parser.add_argument("--baseline", required=True, help="campaign baseline commit")
    parser.add_argument("--supervisor-log", default="")
    parser.add_argument("--upstream", default="",
                        help="branch to measure drift against")
    parser.add_argument("--root", action="append", default=[],
                        help="declared discovery root (repeatable)")
    parser.add_argument("--worktree-root", default="")
    parser.add_argument("--window-hours", type=float, default=0.0)
    parser.add_argument("--out", default="", help="write EVALUATION.json/.md here")
    parser.add_argument("--compare", default="",
                        help="previous scorecard JSON to diff against")
    parser.add_argument("--fail-on-breach", action="store_true",
                        help="exit 1 when any dimension breaches its band")
    return parser.parse_args(argv)


def _write_outputs(card: Scorecard, trend: dict | None, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "EVALUATION.json").write_text(
        json.dumps(card.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "EVALUATION.md").write_text(render_markdown(card), encoding="utf-8")
    if trend:
        (out / "EVALUATION-trend.md").write_text(render_trend(trend), encoding="utf-8")


def _previous_scorecard(path: str) -> Scorecard | None:
    if not path or not Path(path).exists():
        return None
    return scorecard_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    """CLI: score a campaign window, optionally against a previous scorecard."""
    from .campaign_evaluation_facts import evaluate_campaign

    args = _parse_args(argv)
    card = evaluate_campaign(
        state_path=Path(args.state), repo=Path(args.repo),
        baseline=args.baseline,
        supervisor_log=Path(args.supervisor_log) if args.supervisor_log else None,
        upstream=args.upstream, roots=tuple(args.root),
        window_hours=args.window_hours,
        worktree_root=Path(args.worktree_root) if args.worktree_root else None)
    previous = _previous_scorecard(args.compare)
    trend = compare(previous, card) if previous else None

    if args.out:
        _write_outputs(card, trend, Path(args.out))
    else:
        print(render_markdown(card))
        if trend:
            print(render_trend(trend))

    if args.fail_on_breach and card.breaches:
        for flag in card.breaches:
            print(f"breach: {flag.dimension}/{flag.name}: {flag.detail}",
                  file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())