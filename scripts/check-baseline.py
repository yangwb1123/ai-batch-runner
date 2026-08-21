#!/usr/bin/env python3
"""Baseline-first gate (L1, pure stdlib): does the project carry a
machine-verifiable product baseline before claims are made?

A project without a baseline (product architecture / feature coverage /
contracts manifest / roadmap) makes every direction claim unanchored —
snaplink-console and iris-ui both anchored all later decisions on
baseline documents (PRODUCT_ARCHITECTURE + backend-contracts.json /
manifest.json + REQUIREMENTS-BASELINE).

Behavior:
- Scan DIR (default: cwd) for baseline markers:
    product:  PRODUCT_ARCHITECTURE.md / PRODUCT.md / FEATURE_COVERAGE.md
    contract: backend-contracts.json / openapi.yaml / openapi.json /
              manifest.json / docs/evolution/constitution.yaml
    roadmap:  ROADMAP.md / docs/ROADMAP.md
- `--require contract` / `--require roadmap` narrows the check; default
  is advisory (any single baseline marker suffices) — the gate is L1
  suggestion-grade unless the project constitution mandates more.
- Exit 0 = baseline present; exit 1 = missing (stderr feeds the retry
  prompt). `--json` emits {"baseline": {...}} to stdout and keeps exit.

Usage:
    python scripts/check-baseline.py [DIR] [--require product|contract|roadmap]
                                      [--json]
Register as validator: baseline = "python {cwd}/scripts/check-baseline.py {cwd}"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MARKERS = {
    "product": ("PRODUCT_ARCHITECTURE.md", "PRODUCT.md", "FEATURE_COVERAGE.md",
                "docs/PRODUCT_ARCHITECTURE.md", "docs/FEATURE_COVERAGE.md"),
    "contract": ("backend-contracts.json", "openapi.yaml", "openapi.json",
                 "manifest.json", "docs/backend-contracts.json",
                 "docs/evolution/constitution.yaml"),
    "roadmap": ("ROADMAP.md", "docs/ROADMAP.md"),
}


def _scan(root: Path, required: list) -> dict:
    found: dict = {key: [] for key in MARKERS}
    for kind, names in MARKERS.items():
        for name in names:
            candidate = root / name
            if candidate.is_file():
                found[kind].append(name)
    # 无 --require 时是 advisory：三类中任意一类存在即算有基线；
    # 指定 --require 时才按指定类别硬校验。
    if required:
        missing = [kind for kind in required if not found[kind]]
    else:
        missing = list(MARKERS) if not any(found.values()) else []
    return {"found": found, "missing": missing}


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dir", nargs="?", default=".", help="project root (default: cwd)")
    parser.add_argument("--require", action="append", default=[],
                        choices=sorted(MARKERS),
                        help="baseline kinds that MUST exist (repeatable)")
    parser.add_argument("--json", action="store_true", help="emit machine JSON")
    args = parser.parse_args(argv)

    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"check-baseline: not a directory: {root}", file=sys.stderr)
        return 1
    required = args.require or []
    report = _scan(root, required)
    missing = report["missing"]

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    if missing:
        kinds = ", ".join(missing)
        print(f"check-baseline: FAIL — baseline missing for: {kinds}", file=sys.stderr)
        print("Baseline-first (L1): anchor directions on a machine-verifiable "
              "baseline before claiming capability. Create the missing marker(s) "
              "or run `pi-batch evolution init` to scaffold constitution.yaml.",
              file=sys.stderr)
        return 1
    print(f"check-baseline: OK — {len([f for k in MARKERS for f in report['found'][k]])} "
          f"baseline marker(s) present")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
