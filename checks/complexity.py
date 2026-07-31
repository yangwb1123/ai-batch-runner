#!/usr/bin/env python3
"""Complexity gates: cyclomatic (<=15) and cognitive (<=20) per function.

Zero-dependency ast-based implementation standing in for gocyclo/gocognit
(snaplink uses those Go tools; this project keeps the harness portable).

Cyclomatic = 1 + branch points: if/elif, for, while, except, with, ternary
if-expressions, each boolean operator (and/or), each match case.

Cognitive (approximation of the Sonar-style metric) adds a nesting penalty:
a construct nested k levels deep costs 1+k. BoolOps cost their operand
count - 1 and do not increase nesting depth.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checks.config import ComplexityGate, get_config, is_ignored, repo_root

_NESTING_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
    ast.With, ast.AsyncWith, ast.IfExp, ast.Match,
)
_BRANCH_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
                 ast.With, ast.AsyncWith, ast.IfExp)


def _bool_extra(node: ast.BoolOp) -> int:
    return max(0, len(node.values) - 1)


def _cyclomatic_extra(n: ast.AST) -> int:
    """Branch points contributed by one node beyond the base 1."""
    if isinstance(n, ast.Match):
        return len(n.cases)  # each case is a branch (gocyclo switch semantics)
    if isinstance(n, ast.BoolOp):
        return _bool_extra(n)
    return 1


def cyclomatic(node: ast.AST) -> int:
    """Cyclomatic complexity of a function/body node."""
    total = 1
    for n in ast.walk(node):
        if isinstance(n, _BRANCH_NODES):
            total += _cyclomatic_extra(n)
        elif isinstance(n, (ast.Match, ast.BoolOp)):
            total += _cyclomatic_extra(n)
    return total


def cognitive(node: ast.AST) -> int:
    """Approximate cognitive complexity of a function/body node."""
    def visit(n: ast.AST, depth: int) -> int:
        if isinstance(n, ast.BoolOp):
            return _bool_extra(n) + sum(visit(c, depth) for c in ast.iter_child_nodes(n))
        if isinstance(n, ast.Match):
            return len(n.cases) * (1 + depth) + sum(
                visit(c, depth + 1) for c in ast.iter_child_nodes(n))
        inc = 0
        child_depth = depth
        if isinstance(n, _NESTING_NODES):
            inc = 1 + depth
            child_depth = depth + 1
        return inc + sum(visit(c, child_depth) for c in ast.iter_child_nodes(n))
    return visit(node, 0)


def _functions(tree: ast.AST) -> list:
    """Top-level functions and methods (incl. nested defs)."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(n)
    return out


def check_tree(tree: ast.AST, rel: str, gate: ComplexityGate) -> int:
    """Report functions exceeding the budgets. Returns failure count."""
    if is_ignored(rel, gate.ignore_patterns):
        return 0
    failed = 0
    for fn in _functions(tree):
        name = f"{fn.name} @ {rel}:{fn.lineno}"
        cx = cyclomatic(fn)
        if cx > gate.max_cyclomatic:
            print(f"  FAIL: {name} cyclomatic {cx} > {gate.max_cyclomatic}")
            failed += 1
        cg = cognitive(fn)
        if cg > gate.max_cognitive:
            print(f"  FAIL: {name} cognitive {cg} > {gate.max_cognitive}")
            failed += 1
    return failed


def check_file(path: Path, root: Path, gate: ComplexityGate) -> int:
    """Parse one file and check every function. Returns failure count."""
    if path.suffix != ".py":
        return 0
    path = path.resolve()
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        print(f"  FAIL: {rel} unparseable ({e})")
        return 1
    return check_tree(tree, rel, gate)


def run(files: list = None) -> int:
    """Check every .py under the repo root (or the given paths)."""
    root = repo_root()
    gate = get_config().complexity
    if files is None:
        files = sorted(root.rglob("*.py"))
    failed = 0
    for f in files:
        failed += check_file(Path(f), root, gate)
    if failed:
        print(f"  COMPLEXITY: {failed} function(s) exceed budgets "
              f"(cyclomatic {gate.max_cyclomatic}, cognitive {gate.max_cognitive})")
    return failed


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
