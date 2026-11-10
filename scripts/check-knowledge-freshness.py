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
import ast
from collections import Counter
import json
import os
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


def _source_files(root: Path):
    """Yield the existing source scope without entering its excluded trees."""
    excluded = {".dart_tool", "node_modules"}
    if any(part in excluded for part in root.parts):
        return
    for parent, children, names in os.walk(root, followlinks=False):
        children[:] = [name for name in children if name not in excluded]
        for name in names:
            path = Path(parent) / name
            if path.is_file() and path.suffix in _SOURCE_SUFFIXES:
                yield path


def _module_dirs(root: Path) -> list[Path]:
    """Top-level business module directories (depth 1 under root)."""
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in _IGNORE_DIRS:
            continue
        if child.name.startswith("."):
            continue
        if next(_source_files(child), None) is not None:
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



def _python_tree(source: str):
    """Bound optional semantic analysis; uncertainty keeps regex findings."""
    if len(source) > 1048576 or len(source.encode("utf-8")) > 1048576:
        return None
    try:
        tree = ast.parse(source)
        nodes = []
        for node in ast.walk(tree):
            nodes.append(node)
            if len(nodes) > 20000:
                return None
    except (SyntaxError, RecursionError):
        return None
    parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
    return tree, nodes, parents


def _written_names(node) -> list[str]:
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [item.asname or item.name.split(".")[0] for item in node.names]
    # Exception and match-pattern binders store strings rather than Name nodes.
    if isinstance(node, ast.ExceptHandler):
        return [node.name] if node.name else []
    if type(node).__name__ in {"MatchAs", "MatchStar", "MatchMapping"}:
        name = getattr(node, "name", None) or getattr(node, "rest", None)
        return [name] if name else []
    return []


def _path_imports(node) -> dict:
    if isinstance(node, ast.Import):
        return {a.asname or a.name: ("module", node) for a in node.names
                if a.name == "pathlib"}
    if isinstance(node, ast.ImportFrom) and node.module == "pathlib" and not node.level:
        return {a.asname or a.name: ("constructor", node) for a in node.names
                if a.name == "Path"}
    return {}


def _constructor_use(node, kind: str, parents: dict) -> bool:
    parent = parents.get(node)
    if kind == "module":
        if not isinstance(parent, ast.Attribute) or parent.attr != "Path":
            return False
        node, parent = parent, parents.get(parent)
    return isinstance(parent, ast.Call) and parent.func is node


def _binding_counts(nodes: list):
    """Collect every lexical binding and reject explicit dynamic rebinding."""
    counts = Counter(name for node in nodes for name in _written_names(node))
    if "*" in counts or "__file__" in counts:
        return None
    for node in nodes:
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            value = node.value
            while isinstance(value, ast.Attribute):
                value = value.value
            if isinstance(value, ast.Name):
                counts[value.id] += 1
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {"exec", "eval", "globals", "locals", "vars",
                                     "__import__", "setattr", "delattr"}):
            return None
    return counts


def _path_symbols(tree, nodes: list, parents: dict) -> dict:
    counts = _binding_counts(nodes)
    if counts is None:
        return {}
    symbols = {}
    for node in tree.body:
        symbols.update(_path_imports(node))
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            symbols[node.targets[0].id] = ("path", node.value)
    symbols = {name: value for name, value in symbols.items() if counts[name] == 1}
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            kind = symbols.get(node.id, (None,))[0]
            if kind in {"constructor", "module"} and not _constructor_use(node, kind, parents):
                symbols.pop(node.id)
    return symbols


def _earlier_symbol(node, symbols: dict):
    if not isinstance(node, ast.Name):
        return None
    symbol = symbols.get(node.id)
    if symbol and (symbol[1].lineno, symbol[1].col_offset) < (node.lineno, node.col_offset):
        return symbol
    return None


def _path_constructor(node, symbols: dict) -> bool:
    if isinstance(node, ast.Name):
        symbol = _earlier_symbol(node, symbols)
        return bool(symbol and symbol[0] == "constructor")
    if isinstance(node, ast.Attribute) and node.attr == "Path":
        symbol = _earlier_symbol(node.value, symbols)
        return bool(symbol and symbol[0] == "module")
    return False


def _path_expression(node, symbols: dict, depth: int = 0) -> bool:
    if depth > 16:
        return False
    if isinstance(node, ast.Name):
        symbol = _earlier_symbol(node, symbols)
        return bool(symbol and symbol[0] == "path"
                    and _path_expression(symbol[1], symbols, depth + 1))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
                and _path_expression(node.left, symbols, depth + 1))
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return _path_expression(node.value, symbols, depth + 1)
    return _path_call(node, symbols, depth)


def _path_call(node, symbols: dict, depth: int) -> bool:
    if not isinstance(node, ast.Call) or node.keywords:
        return False
    if _path_constructor(node.func, symbols):
        return all(isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                   or isinstance(arg, ast.Name) and arg.id == "__file__" for arg in node.args)
    return (not node.args and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"resolve", "absolute"}
            and _path_expression(node.func.value, symbols, depth + 1))


def _readonly_path(uses: list, parents: dict) -> bool:
    if not uses:
        return False
    for node in uses:
        parent = parents.get(node)
        if not isinstance(parent, ast.Attribute) or parent.attr not in {"read_text", "read_bytes"}:
            return False
        call = parents.get(parent)
        if not isinstance(call, ast.Call) or call.func is not parent:
            return False
    return True


def _readonly_path_spans(source: str) -> list[tuple[int, int]]:
    parsed = _python_tree(source)
    if parsed is None:
        return []
    tree, nodes, parents = parsed
    symbols = _path_symbols(tree, nodes, parents)
    lines = source.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    reads = {}
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            reads.setdefault(node.id, []).append(node)
    spans = []
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        target = node.targets[0]
        if (symbols.get(target.id) != ("path", node.value)
                or not _path_expression(node.value, symbols)
                or not _readonly_path(reads.get(target.id, []), parents)):
            continue
        line = lines[target.lineno - 1].encode("utf-8")
        start = offsets[target.lineno - 1] + len(line[:target.col_offset].decode("utf-8"))
        end = offsets[target.end_lineno - 1] + len(line[:target.end_col_offset].decode("utf-8"))
        spans.append((start, end))
    return spans


def _has_critical_assignment(path: Path, source: str) -> bool:
    if not _CRITICAL_ASSIGN.search(source):
        return False
    spans = _readonly_path_spans(source) if path.suffix == ".py" else []
    if not spans:
        return True
    return any(not any(start <= match.start() < end for start, end in spans)
               for match in _CRITICAL_ASSIGN.finditer(source))


def _check_critical_number_audit(root: Path) -> list[dict]:
    """Trust: critical-number mutations need audit signals in the file."""
    findings = []
    for path in _source_files(root):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Only flag when a critical field is actually mutated.
        if not _has_critical_assignment(path, source):
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
