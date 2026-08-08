Design complete and verified. Here's the summary:

## Verification results — all material evidence claims confirmed live

I re-ran every claim in the requirements evidence against the actual tree before designing:

**Confirmed exact**: `eval.py:89` (`asserts = case.get("assert", {})` — only assert keys executed), `:120` `_suite_domains` (expect = display only), `:79` `ValueError` for unknown keys; `classifier.py:206`/`:190`/`:119`/`:247`; `rule_matcher.py:202`/`:243`/`:240`; `rules.yaml` expect at 75/86/94/104; `Makefile:40-41`; `test_entrypoints.py:52-68`.

**Confirmed live**: `eval --json` → `total: 41, failed: 0` (direction's "40" corrected correctly); `--quick` → 35; all 6 classifier `system_type` expectations match actual `classify_text` output; `--filter system-type-state-machine` currently returns `total: 0` (name-keyed, untargetable); both divergent rules cases reproduced — ORIG workbench → `tier=standard`, 0 rules; ORIG backend ("含支付审批流程") → `classify_text = frontend_ui` with zero of the 6 backend di ids. Both corrected inputs produce exactly the expected manifests (all 6 di ids each), and both are byte-identical to strings already pinned in `tests/test_design_intelligence.py:96/106`.

**Minor drift only** (non-material): parentheticals off by one line (`rules.yaml:69`/`:100`); stale "25 cases" doc counts in AGENTS.md/SPEC_SYSTEM.md/selfcheck docstring — no test depends on them.

**New finding**: `run_suite`/`_assert_value` have exactly one consumer (`cli.py:302`); selfcheck shells out via subprocess — no hidden API coupling.

## Design deliverable

**`docs/auto/design/evals-85368cf1-design.md`** (following the `checks-d4b833b0-design.md` convention) contains:

- **API changes** — two layers: (1) `pbatch/eval.py` only: `EXECUTABLE_EXPECT_KEYS = (system_type, tier, includes, excludes)`, expect-as-fallback in `run_suite`, `system_type` added to the classify_text dispatch branch, `includes`/`excludes` sharing `has_rule`/`lacks_rule` set semantics, `VacuousCaseError` → `eval_main` exit 2; (2) `evals/*.yaml` only: 10 × `name:`→`id:` renames, 2 input corrections (expectations byte-identical).
- **Compatibility constraints** — exit-code contract (0/1/2, matching `load_eval_files`' existing exit 2), JSON shape, `--quick`/`--filter` semantics, assert-wins precedence, zero classifier/rule_matcher changes, collision-free ids.
- **Failure modes** — F1–F8 table: vacuous-false-positive (intended fail-closed), keyword-drift re-routing (loud exit 1 — the point), includes superset semantics, `assert: {}` edge, `--filter` typo silent no-op, exit-2-without-JSON-payload asymmetry.
- **Migration steps** — 6 ordered, independently verifiable steps (machinery → classifier data → rules data → tests → full verification → commit).
- **Acceptance mapping** — A1–A4/R6/R7 → concrete commands (`--filter` ×5, `make eval`, pytest) with expected `total`/`failed`/exit codes, plus 5 new `tests/test_eval_suite.py` cases (monkeypatched `EVAL_DIR`, real `evals/` untouched).
- **Out of scope** — classifier routing changes, doc-count sync (recommended follow-up), `--filter` silent no-op.

Post-change projection verified by construction: all 10 formerly-vacuous cases pass with real assertions → `total: 41, failed: 0`, exit 0, `make ci` green.
