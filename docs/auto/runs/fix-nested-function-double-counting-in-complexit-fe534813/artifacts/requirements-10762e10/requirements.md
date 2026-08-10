Requirements specification produced at `docs/auto/runs/fix-nested-function-double-counting-in-complexit-fe534813/artifacts/requirements-10762e10/requirements.md`.

**Evidence verification (all 6 citations confirmed live, no line shifts):**
- `checks/complexity.py:73-79` — `_functions()` collects nested defs via `ast.walk` over the whole tree ✓
- `checks/complexity.py:48-52` — `cyclomatic()` walks the full subtree via `ast.walk(node)` ✓
- `checks/complexity.py:82-89` — `check_tree()` iterates `_functions()` and calls `cyclomatic(fn)` ✓
- `checks/complexity.py:93-117` — `cognitive(fn)` call (line 93) + surrounding flow ✓
- `checks/config.py:165-167` — `is_ignored()` blunt substring match ✓
- `checks/test_complexity.py:9-14` — imports + `_fn` helper (multi-line bodies already supported) ✓

**Empirical reproduction (run live):** outer cyclomatic=4/cognitive=6, inner 4/6 (double count confirmed); baseline `checks/` = 49 passed; `cli.py check-complexity` exits 0.

**Feasibility validated, not committed:** a prune-aware traversal prototype (skip `FunctionDef`/`AsyncFunctionDef` subtrees at any depth) yields exactly the target semantics — wrapper 1/0, closure 4/6, plus decorator factories, async wrappers, class methods, and def-inside-`if` cases all score per-unit; flat fixtures unchanged.

**Spec contents:** root-cause analysis (two full-tree walks combining into double counting), 7 functional requirements, explicit out-of-scope boundaries (`ast.Lambda`, cognitive metric definition, config/ignore_patterns, other gates), and the 3 supplied acceptance checks preserved and made testable — AC1 as a concrete regression test with exact assertions and expected values, AC2 pinned to 50 tests (49 existing + 1 new; the 14 existing tests in test_complexity.py stay unmodified), AC3 with a monotonicity argument (fix only decreases scores ⇒ exit 0 guaranteed) plus verification commands.
