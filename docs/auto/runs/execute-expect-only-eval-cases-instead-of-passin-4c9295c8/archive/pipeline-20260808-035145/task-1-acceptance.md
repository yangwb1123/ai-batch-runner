All 19 failures show the "Another pi-batch instance is running" lock error (exit 5, pid 1684832 = the campaign orchestration itself holding `.pi-batch.lock`) — purely environmental, zero eval-related failures. The verification is complete.

## Verification summary

| Acceptance | Check | Result |
|---|---|---|
| **A1** — `system_type` key really checks `state-machine` | `eval --filter system-type-state-machine --json` → `total: 1, failed: 0` rc 0; effective assertion set = `{system_type}` (probed, non-empty) | ✅ |
| **A2** — 4 rules cases execute real assertions | `--filter` × all 4 ids → each `total: 1, failed: 0` rc 0; assertion sets = `{includes, tier}` / `{includes, excludes}` etc. | ✅ |
| **A3** — vacuous case fails closed | Temp `EVAL_DIR` case with only `expect.task_type` → exit **2**, error names `tmp-suite/bare-expect`, **zero stdout bytes** (no JSON payload) | ✅ |
| **A4** — 41 cases, 10 now real | `eval --json` → `total: 41, failed: 0, failures: []` rc 0; probe confirms **none** of the 10 renamed cases is vacuous | ✅ |
| **R1/R2** — expect-fallback + 3 new keys | `_effective_asserts` (assert wins, `{}` treated absent) + `_assert_value` dispatch (`system_type`→`classify_text`, `includes`/`excludes`→`match_rules` rule-id sets) | ✅ (code + tests) |
| **R3** — exit 2 | `VacuousCaseError` caught per-suite in `eval_main`, names suite/case | ✅ |
| **R4/R5** — data | git diff: 6 classifier `name:`→`id:` byte-identical; 4 rules renames + exactly the 2 documented input corrections (workbench, backend "流程" removed), expectations byte-identical | ✅ |
| **R6** — suite green | `make eval` rc 0; `--quick` → 35/0; `test_entrypoints.py::test_eval_quick_mode_runs_core_domains` passes; `pi-batch check` 8/8 gates; `make ci` → 951 passed, 19 failed — **all 19** are exit-5 lock contention (`Another pi-batch instance is running (pid=1684832)`) in `test_pi_batch.py`/`test_local_fixes.py`, confirmed environmental (live campaign process holds `.pi-batch.lock`) | ✅ (env-blocked, eval-unrelated) |
| **R7** — new pytest coverage | `tests/test_eval_suite.py` → **17 passed** (superset of R7's 3: dispatch pass/fail ×2, vacuous exit 2 + no-JSON, assert-`{}` fallback, assert-wins, parametrized 10-id filter pin on real `evals/`, 2 schema guards); full `tests/` sweep minus lock files → **767 passed, 1 skipped**; `test_design_intelligence` 14/14, `test_backend_experience` 12/12, `test_rule_matcher`+`test_classifier` 58/58 | ✅ |
| Quality gates | `quality.py pbatch/eval.py` OK (F-1 blocker resolved: `run_suite` 9, `eval_main` 10 ≤ 15); `quality.py --strict .` OK | ✅ |

VERDICT: PASS - all acceptance checks A1–A4 and requirements R1–R7 are satisfied with live re-executed evidence: the 10 formerly-vacuous cases (6 classifier `system_type` + 4 rules `includes`/`excludes`/`tier`) now execute real assertions (`eval --json` → 41/0 rc 0, each filter-targetable `total: 1`), vacuous cases fail closed with exit 2 naming suite/case and no JSON payload, all 17 `test_eval_suite.py` regression tests plus the full eval-related suite pass, data diffs are exactly the specified renames + 2 input corrections, quality gates are green, and the only `make ci` failures (19) are provably environmental exit-5 lock contention from the live campaign process holding `.pi-batch.lock` — unrelated to this change.
