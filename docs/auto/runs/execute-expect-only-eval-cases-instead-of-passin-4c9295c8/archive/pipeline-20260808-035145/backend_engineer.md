Implementation complete. Summary of what landed:

## 1. Analysis
- **Module boundary**: eval subsystem only — machinery (`pbatch/eval.py`) + case data (`evals/classifier.yaml`, `evals/rules.yaml`). Zero changes to classifier/rule_matcher/assess.yaml.
- **Data ownership**: exit-code contract is the public API (0 pass / 1 assertion fail / **2 new**: schema-vacuous, consistent with `load_eval_files`); JSON shape, `--quick`, `--filter` unchanged.
- **Strong consistency**: a case can never pass with zero executed assertions — `VacuousCaseError` (names suite/case) → exit 2, per-suite.
- **Pattern decision**: **no pattern — composition suffices** (key-filter + one shared list-semantics helper reusing `has_rule`/`lacks_rule` set logic). The 2-state assert-vs-expect precedence is centralized in `_effective_asserts` — below the >3-state threshold, no full state machine warranted.

## 2. Implementation
- **`pbatch/eval.py`**: `EXECUTABLE_EXPECT_KEYS = (system_type, tier, includes, excludes)`; `VacuousCaseError`; `system_type` in the classify_text dispatch; `includes`/`excludes` sharing rule-id set semantics; expect-as-fallback with assert-wins precedence; `eval_main` → exit 2 on vacuous; docstring updated with the exit-code contract. First draft tripped the repo's own complexity gate (18 > 15) — refactored into `_effective_asserts` + `_list_assertion_failure` (run_suite now 8), which `test_check_subcommand_passes` caught and now confirms green.
- **`evals/*.yaml`**: 10 × `name:`→`id:` renames (all collision-free), 2 divergent inputs corrected to the strings already pinned in `test_design_intelligence.py`.
- **`tests/test_eval_suite.py`**: 5 new tests (system_type dispatch, includes/excludes dispatch, vacuous→exit 2, assert-wins precedence, real-suite `--filter` A1), monkeypatched `EVAL_DIR`, real `evals/` untouched.
- Report: `docs/auto/runs/execute-expect-only-eval-cases-instead-of-passin-4c9295c8/artifacts/implement-f9bb818d/task-1-implementation.md`

## 3. Self-check & verification
- architecture.md §8/§9, evolution.md §6 checklist: all pass (single boundary, no new dependency edges, no abstractions, budget verified by gate).
- **Run**: 5/5 new tests; A1 + A2 ×4 filters → each total 1 failed 0; `eval --json` → **41/0**; `--quick` → 35; `make eval` exit 0; entrypoints quick-mode ✓; 107 eval-adjacent tests ✓; `quality.py --strict .` OK; `make ci` → 939 passed.
- **Could not fully run green**: `make ci` has 19 pre-existing failures in `test_pi_batch.py`/`test_local_fixes.py` (CLI-subprocess tests) — verified **identical failure set and count (19/166) with my changes stashed**, i.e. caused by other in-flight campaign changes in this tree, not this direction. `git commit` left to the campaign flow (tree carries concurrent uncommitted work).
