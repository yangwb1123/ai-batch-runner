Requirements spec written. Summary of what was produced and verified:

**Evidence verification (all 5 citations confirmed live, no line shifts):**
- `pbatch/eval.py:164` — `run_suite` silently `continue`s on filter miss, no match tracking
- `pbatch/eval.py:235` — `eval_main` CLI entry
- `pbatch/eval.py:261` — `total += 1` only counts executed cases → total=0 on miss
- `pbatch/eval.py:275` — `sys.exit(1)` unreachable when `failed` empty → falls through to exit 0
- `tests/test_eval_suite.py:87` — crafted `- id: di-login` wrong-expectation case proving a run-and-fail filter is loud (exit 1); only the zero-match path is silent

**Live reproduction:** `--filter no-such-case --json` → `{"total":0,"failed":0}`, exit 0 (fail-open confirmed); `--filter system-type-state-machine` → total=1, exit 0; no filter → total=42, exit 0. `make eval` = `python -m pbatch eval` (Makefile:41).

**Spec contents:**
- Root-cause analysis (three-line chain: `continue` → `total` never increments → `if failed:` empty → fallthrough to "all 0 passed")
- 6 functional requirements (FR1–FR6), including fail-closed exit 2 with message naming the filter, F-7 no-JSON-payload consistency, exact-match semantics preserved, and the guard formulated on the *executed* case set so `--quick` variants of the same hole are closed by one rule
- All 3 supplied acceptance checks preserved and made testable: AC1 as unit + subprocess regression tests, AC2 already pinned by the existing parametrized `test_filter_targets_renamed_cases` (must pass unmodified), AC3 pinned on the make target's command body
- Explicit out-of-scope boundaries (coverage display path, `--quick` suite selection, fuzzy matching, exit-code renumbering, the two sibling directions in the same analysis)
- Design-stage notes: ~3-line surface in `eval_main`, no existing test uses a non-matching filter, exit-code discrimination preserved for CI (0=ran+passed, 1=ran+failed, 2=executed nothing)

Output: `docs/auto/runs/eval-filter-typo-passes-silently-fail-open-in-ci-68b869ba/artifacts/requirements-10762e10/requirements.md`
