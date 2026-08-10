"""Command-line adapter for the deterministic rule matcher."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .classifier import classify_text
from .rule_matcher import (_check_registries_cli, _reconcile_cli, domain_for,
                           format_llm_prompt, format_manifest, load_registry,
                           match_rules, summarize_task)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch.py rules",
        description="Match frontend/backend spec rules to a task and reconcile "
                    "the LLM's independent selection (two-sided check).")
    parser.add_argument("task", nargs="*", default=[], help="task prompt text")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--summary", action="store_true",
                        help="print the compressed requirement (LLM input)")
    parser.add_argument("--llm-prompt", default="",
                        help="write the two-sided-check prompt to FILE")
    parser.add_argument("--llm-json", default="",
                        help='LLM selection JSON {"apply":[],"skip":[]} to reconcile')
    parser.add_argument("--registry", default="", help="rule registry YAML override")
    parser.add_argument("--check", action="store_true",
                        help="validate registry schema integrity (both domains)")
    return parser


def _registry(text: str, override: str, parser: argparse.ArgumentParser):
    classification = classify_text(text)
    try:
        return (load_registry(override, domain_for(text, classification))
                if override else None)
    except ValueError as exc:
        parser.error(str(exc))


def rules_main(argv: list) -> None:
    """Match deterministic rules and optionally reconcile an LLM selection."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.check:
        _check_registries_cli()
        return
    text = " ".join(args.task)
    if not text.strip():
        parser.error("Provide a task (positional text) or --check")
    registry = _registry(text, args.registry, parser)
    if args.summary:
        print(summarize_task(text, registry))
        return
    if args.llm_prompt:
        target = Path(args.llm_prompt)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(format_llm_prompt(text, registry), encoding="utf-8")
        print(f"LLM check prompt written to {target}")
        return
    if args.llm_json:
        _reconcile_cli(text, args.llm_json, registry, args.json)
        return
    matched = match_rules(text, registry=registry)
    if args.json:
        print(json.dumps(matched, ensure_ascii=False, indent=2))
        return
    print(format_manifest(matched))
