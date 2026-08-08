All gates verified. Here is the implementation report.

---

## 1. Analysis (mandatory thinking order)

| Question | Answer |
|---|---|
| **Business boundary** | `pbatch/eval.py` owns the rule-system regression suite (`evals/*.yaml`). The fix belongs entirely in `eval_main` — the CLI entry that consumes `run_suite` results. |
| **Data ownership** | `run_suite(suite, only_id)` owns exact-match filter semantics; `eval_main` owns the run outcome contract (exit codes, JSON payload). No new data, no new contract surface. |
| **Strong consistency** | None needed — this is a CLI process, not a transactional system. The only invariant: an executed-case count of 0 must never be reported as a successful run. |
| **Change points** | One: "executed nothing" was silently treated as "all passed". Nothing else varies. **No abstraction** — the decision table (design-patterns.md) matches no pattern here; composition (guard on the existing `total` counter) suffices. |
| **Dependency direction** | Unchanged: `eval_main` → `run_suite` → analyzers. No new imports, no framework coupling. |
| **Persistence gate** | Not applicable — no entities, no tables, no money/status fields. `evals/*.yaml` inputs untouched (42 cases unchanged). |
| **Production readiness** | Fail-closed guard + stderr message naming the filter + exit-code discrimination (0=ran+passed / 1=ran+failed / 2=executed nothing) + F-7 stdout contract (exit 2 never emits JSON) — all verified live. |

## 2. Implementation

**`pbatch/eval.py` — one guard in `eval_main`** (after `--coverage` branch, before JSON print; the design's exact 6-line body):

```python
    if total == 0:
        # Fail closed: zero executed cases = usage error, not "all 0 passed".
        if args.filter:
            log.error("EVAL: --filter %r matched no eval cases (exact case id "
                      "required; see evals/*.yaml)", args.filter)
        else:
            log.error("EVAL: executed 0 eval cases (no evals/*.yaml loaded?)")
        sys.exit(2)
```

Plus module docstring contract: `2 = schema/vacuous-case error OR --filter matched zero cases … Exit 2 never emits a JSON payload (F-7)`. `run_suite` untouched; JSON shape unchanged; no CLI/config/evals changes.

**`tests/test_eval_suite.py` — 5 new regression tests** (13 total in file):
- `test_filter_no_match_exits_2` (AC1a): unit, exit 2, stderr names filter, no `{` on stdout
- `test_filter_no_match_subprocess_exits_2` (AC1b): real evals/, `--filter no-such-case --json` → rc 2, stdout clean, stderr names filter
- `test_quick_filter_hitting_skipped_suite_exits_2` (FR-4a): `--quick --filter l3-prescription-no-billing-without-signal` (assess suite skipped) → exit 2
- `test_quick_filter_matching_core_case_still_passes` (FR-4b): `--quick --filter system-type-state-machine` → exit 0, `{"total":1,…}`
- `test_empty_eval_dir_exits_2` (Edge): empty evals/ → exit 2, not "all 0 passed"

## 3. Self-check & verification

**architecture.md §8 (state management)**: N/A — no state machines in this change; the closest analog (exit-code "state" transitions) is centralized in `eval_main`, not scattered.
**evolution.md §6 checklist**: boundary clear (eval CLI) ✓; no new dependencies/cycles ✓; no framework coupling ✓; no god files introduced (eval_main trimmed back to 50-line budget) ✓; no cross-module access ✓; no one-implementation abstraction ✓; no pattern without documented problem (none introduced) ✓; independently testable (13 tests, monkeypatched EVAL_DIR + read-only subprocess) ✓.

**Commands run** (all in `/home/u1/ai-batch-runner`):

| Command | Result |
|---|---|
| `python -m pytest tests/test_eval_suite.py tests/test_context_eval.py tests/test_entrypoints.py` | ✅ 39 passed |
| `make eval` | ✅ exit 0, all 42 passed |
| Live behavior matrix (`--filter typo` → exit 2/empty stdout/stderr names filter; valid filter → total=1; `--quick`+skipped → exit 2; `--quick`+matching → total=1; `--coverage`+typo → unchanged) | ✅ all match design §3.1 |
| `python quality.py --strict .` | ✅ OK |
| `make ci` | ⚠️ **1014 passed / 22 failed — pre-existing environmental**: all 22 `test_pi_batch.py` failures are `exit 5: Another pi-batch instance is running (pid=1547473…)` — the campaign process itself holds the repo lock. Reproduced identically with my changes stashed (base tree), proving they're not caused by this change. Could not fully run in this environment; will pass when the campaign releases the lock. |

**Could not run:** full `make ci` green (blocked by the running campaign's repo lock, not by this change).
