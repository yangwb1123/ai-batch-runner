All design claims are now validated live. The design is complete and written to the pipeline's expected output.

## Summary

**Design written to:** `docs/auto/runs/fix-nested-function-double-counting-in-complexit-fe534813/artifacts/design-a77de8a6/task-1-design.md`

### Evidence verification (all untrusted claims independently confirmed)
- ✅ All 6 line citations confirmed live with no shifts (`complexity.py:73-79` `_functions`, `:48-52` cyclomatic walk, `:82-89` check_tree, `:93-117` cognitive flow; `config.py:165-167` `is_ignored`; `test_complexity.py:9-14` `_fn` helper)
- ✅ Empirical: analysis fixture reproduces exactly — outer cyclomatic=4/cognitive=6, inner 4/6 (double count real)
- ✅ Baseline: `pytest checks/` = 49 passed; `cli.py check-complexity` exits 0
- ✅ Feasibility: re-derived the prune-aware prototype independently and validated an 9-case matrix (wrapper 1/0 + closure 4/6, async, decorator factories, class methods, def-inside-`if`/`match`, depth-3, decorator-expr attribution, flat unchanged) plus all 9 metric expectations of the 14 existing tests
- ⚠️ One note: the on-disk requirements artifact is only a 15-line summary; FRs were re-enumerated in the design to be self-contained

### Design decisions
- **Approach**: single `_own_children()` child filter used by both metrics — `cyclomatic` becomes an iterative pruned walk (avoids recursion regression where today's `ast.walk` is iterative), `cognitive` swaps `iter_child_nodes` → `_own_children` in all three return paths (BoolOp/Match/generic). `_functions()` deliberately **unchanged** — the highest-risk wrong fix (returning top-level only) would silently stop scoring nested defs (fail-open); explicitly called out with a check_tree-level regression assertion to pin it.
- **API changes**: zero public changes; two private helpers + docstring; no config/CLI/validator changes
- **Failure modes**: 6 enumerated with detection (partial cognitive fix, wrong-layer fail-open, over-prune, decorator misattribution, recursion, count drift)
- **Migration**: none for consumers; single-commit rollback; step-by-step implementation sequence
- **Acceptance mapping**: AC1 → new single test method with subTests (exact 1/0 + 4/6 assertions, both metrics, plus check_tree-level no-fail-open pin); AC2 → 15 passed in test_complexity.py / 50 in checks/ with 14 unmodified; AC3 → monotonicity argument + live exit-0 run, no ignore_patterns diff
