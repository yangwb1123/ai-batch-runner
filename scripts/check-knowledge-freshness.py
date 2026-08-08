#!/usr/bin/env python3
"""Knowledge Maintenance gate (pure stdlib).

Maintenance Intelligence, layer 6: 知识维护是最易忽略的维护层——
每个业务模块需要"现状文档 + 决策记录"才可被接手/演化。

Deterministic checks:

1. Module documentation — top-level business module directories
   (containing source files) should have a README / docs sibling or a
   doc file inside; a source-only module with zero docs is a
   knowledge-maintenance signal.
2. Decision records — repos with non-trivial business logic should
   have an ADR/docs directory (architecture knowledge decays without
   written decisions).
3. Data-truth signals — code touching critical numbers (amount/inventory/
   balance/price) without any audit/version/history reference nearby is
   a trust risk (complements check-backend-experience's write-audit).

Scans any source tree (Go/Dart/TS/Python/Java/Rust). Exits 0 clean,
1 violations, 2 usage error. `--json` emits a report.

Usage:
  python check-knowledge-freshness.py [--dir DIR] [--json] [-o OUT]
  python check-knowledge-freshness.py --selfcheck
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Heuristics
# --------------------------------------------------------------------------

_SOURCE_SUFFIXES = {".go", ".dart", ".ts", ".tsx", ".py", ".js", ".java", ".rs"}
_DOC_NAMES = re.compile(
    r"(readme|README|\.md$|docs?/|ADR|architecture|design|spec|contract)",
)
_IGNORE_DIRS = {
    ".git", ".dart_tool", "node_modules", "build", "dist", "vendor",
    "coverage", ".idea", ".vscode", "test", "tests", "scripts", "tools",
}

# Critical-number fields: mutations to them need audit/version/history.
_CRITICAL_FIELD = re.compile(
    r"(?:amount|balance|price|inventory|stock|salary|"
    r"余额|金额|库存|价格|单价)",
    re.IGNORECASE,
)
_AUDIT_SIGNAL = re.compile(
    r"audit|version|history|changelog|updated_at|updatedAt|revision|event\b|"
    r"审计|历史|版本|流水|日志",
    re.IGNORECASE,
)
_CRITICAL_ASSIGN = re.compile(
    r"(?:amount|balance|price|inventory|stock|salary|余额|金额|库存|价格|单价)"
    r"\w*\s*(?:\+=|-=|\*=|/=|=)",
    re.IGNORECASE,
)


def _module_dirs(root: Path) -> list[Path]:
    """Top-level business module directories (depth 1 under root)."""
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in _IGNORE_DIRS:
            continue
        if child.name.startswith("."):
            continue
        sources = [
            p for p in child.rglob("*")
            if p.is_file() and p.suffix in _SOURCE_SUFFIXES
            and ".dart_tool" not in p.parts and "node_modules" not in p.parts
        ]
        if sources:
            out.append(child)
    return out


def _check_module_docs(root: Path, skip_modules: set[str] | None = None) -> list[dict]:
    """Layer 6: modules without any documentation."""
    skip_modules = skip_modules or set()
    findings = []
    for module in _module_dirs(root):
        if module.name in skip_modules:
            continue
        has_docs = False
        for p in module.rglob("*"):
            if p.is_file() and (
                p.name.lower() in {"readme.md", "readme", "index.md"}
                or p.suffix == ".md"
                or "docs" in p.parts
            ):
                has_docs = True
                break
        if not has_docs:
            findings.append({
                "file": str(module),
                "check": "module_without_docs",
                "detail": (
                    f"{module}: source module without README/docs — "
                    "knowledge maintenance: modules need 现状文档 + 决策记录"
                ),
            })
    return findings


def _check_adr_presence(root: Path) -> list[dict]:
    """Decision records: business repos should have an ADR/docs dir."""
    if not _module_dirs(root):
        return []
    has_adr = any(
        p.is_dir() and p.name.lower() in {"docs", "adr", "decisions", "doc"}
        for p in root.iterdir()
    )
    if has_adr:
        return []
    return [{
        "file": str(root),
        "check": "no_decision_records",
        "detail": (
            f"{root}: no ADR/docs directory — architecture knowledge "
            "decays without written decisions (evolution)"
        ),
    }]


def _check_critical_number_audit(root: Path) -> list[dict]:
    """Trust: critical-number mutations need audit signals in the file."""
    findings = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        if ".dart_tool" in path.parts or "node_modules" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Only flag when a critical field is actually mutated.
        if not _CRITICAL_ASSIGN.search(source):
            continue
        if _AUDIT_SIGNAL.search(source):
            continue
        # 假阳性排除：设备清单/织网配置中的 inventory 是"设备清单"
        # 语义，不是财务库存（config.py 的 _DEVICE_INVENTORY 案例）。
        if ("_DEVICE_INVENTORY" in source
                or "device_fabric" in source and "inventory" in source):
            continue
        findings.append({
            "file": str(path),
            "check": "critical_number_without_audit",
            "detail": (
                f"{path}: critical fields (amount/inventory/price) "
                "mutated without audit/version/history signals — "
                "trust requires 谁/何时/改了什么/为什么"
            ),
        })
    return findings


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _selfcheck() -> int:
    import tempfile

    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Clean repo: module with README + docs dir.
        (root / "orders").mkdir()
        (root / "orders" / "service.go").write_text(
            "func Update(o Order) { db.Update(o) }"
        )
        (root / "orders" / "README.md").write_text("# Orders")
        (root / "docs").mkdir()
        (root / "docs" / "adr-001.md").write_text("# ADR 001")
        if _check_module_docs(root):
            bad += 1
            print("selfcheck FAIL: documented module flagged")
        if _check_adr_presence(root):
            bad += 1
            print("selfcheck FAIL: ADR dir not recognized")

        # Module without docs.
        (root / "inventory").mkdir()
        (root / "inventory" / "stock.go").write_text("package inventory")
        docs = [v for v in _check_module_docs(root) if "inventory" in v["file"]]
        if not docs:
            bad += 1
            print("selfcheck FAIL: undocumented module not flagged")

        # Critical number without audit.
        (root / "finance").mkdir()
        (root / "finance" / "account.go").write_text(
            "balance = balance + 100"
        )
        critical = [
            v for v in _check_critical_number_audit(root)
            if "finance" in v["file"]
        ]
        if not critical:
            bad += 1
            print("selfcheck FAIL: critical number without audit not flagged")

    print("selfcheck:", "OK" if bad == 0 else f"{bad} FAILURES")
    return 0 if bad == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=".", help="directory to scan")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-o", "--output", help="write report to file")
    parser.add_argument("--selfcheck", action="store_true")
    parser.add_argument(
        "--skip", default="", help="comma-separated path fragments"
    )
    parser.add_argument(
        "--skip-modules", default="",
        help="comma-separated top-level module names exempt from doc checks"
    )
    args = parser.parse_args(argv)

    if args.selfcheck:
        return _selfcheck()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"error: {args.dir} is not a directory", file=sys.stderr)
        return 2

    skipped = [s for s in args.skip.split(",") if s]
    skip_modules = {s for s in args.skip_modules.split(",") if s}
    violations = [
        v for v in (
            _check_module_docs(root, skip_modules)
            + _check_adr_presence(root)
            + _check_critical_number_audit(root)
        )
        if not any(s in v["file"] for s in skipped)
    ]

    report = {
        "directory": args.dir,
        "violations": violations,
        "total": len(violations),
    }
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for v in violations:
            print(v["detail"])
        print(f"{len(violations)} violation(s) found")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
