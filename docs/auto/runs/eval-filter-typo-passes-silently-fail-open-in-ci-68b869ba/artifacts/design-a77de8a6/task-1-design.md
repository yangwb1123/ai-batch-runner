Design artifact written: `docs/auto/runs/eval-filter-typo-passes-silently-fail-open-in-ci-68b869ba/artifacts/design-606312fc/design.md`

## Evidence verification — all claims confirmed live

| Claim | Result |
|---|---|
| `eval.py:164` run_suite `continue` on filter miss | ✅ (continue at 169–170) |
| `eval.py:235` eval_main | ✅ |
| `eval.py:261` `total += 1` only for executed cases | ✅ |
| `eval.py:275` `sys.exit(1)` unreachable when `failed` empty | ✅ falls through to "all 0 passed", exit 0 |
| `test_eval_suite.py:87` wrong-expect `di-login` → exit 1 | ✅ (line 87, asserts exit 1 at 94) |
| Live: `--filter no-such-case --json` → `{"total":0,"failed":0}` exit 0 | ✅ fail-open reproduced |
| Live: `--filter system-type-state-machine` → total=1; no filter → total=42; Makefile:41 | ✅ |

Also verified: `log.error` → stderr (no `stream=` in basicConfig), so exit-2 paths keep stdout clean; no existing test uses a non-matching filter; all consumers (`make eval`, `test_context_eval.py`, `test_entrypoints.py:59`, AC2 parametrized test) use matching/unfiltered runs — unaffected.

## Design (per requirements constraints)

**Single 6-line guard in `eval_main`, placed after the `--coverage` branch, before the JSON print:**

```python
    if total == 0:
        if args.filter:
            log.error("EVAL: --filter %r matched no eval cases (exact case id "
                      "required; see evals/*.yaml)", args.filter)
        else:
            log.error("EVAL: executed 0 eval cases (no evals/*.yaml loaded?)")
        sys.exit(2)
```

**API changes:** only `eval_main` (guard) + module docstring exit-code contract (2 = schema error **or** filter matched zero cases; exit 2 never emits JSON — F-7). `run_suite` untouched (exact-match semantics preserved), JSON payload shape unchanged, no CLI/config/evals changes.

**Compatibility:** exit-code discrimination 0=ran+passed / 1=ran+failed / 2=executed nothing preserved; stdout empty on exit 2 (same contract as existing vacuous-case path); guard reads the *executed* count so `--quick` + filter-hitting-skipped-suite exits 2 via the same rule; `--coverage` display path, fuzzy matching, exit renumbering, sibling directions explicitly out of scope.

**Failure modes:** typo/partial-match filter → exit 2 naming filter; match+FAIL → exit 1 with JSON (unchanged, loud); vacuous case → exit 2 (unchanged); empty `evals/` dir → exit 2 (was silent pass, same bug class); duplicate ids → total=N (pre-existing, unchanged).

**Migration:** apply guard → docstring → add tests → `pytest tests/test_eval_suite.py tests/test_context_eval.py tests/test_entrypoints.py` → `make eval` → `make ci` → `quality.py --strict .`. No data/config/evals migration.

**Acceptance mapping:** AC1a/AC1b new unit + subprocess tests (exit 2, stderr names filter, no stdout JSON); AC2 existing `test_filter_targets_renamed_cases` passes unmodified; AC3 `make eval` stays exit 0/42; FR-4a/4b `--quick` hole-closure tests; FR-2 exit-1-with-JSON already pinned at `test_eval_suite.py:83-94`; F-7 asserted in both exit-2 paths.
