# Design: `eval --filter` no-match fails closed (exit 2)

Direction: eval-filter-typo-passes-silently-fail-open-in-ci-68b869ba
Stage: design · Input: `requirements-10762e10/requirements.md`

## 1. Evidence verification (re-verified live at design time, no line shifts)

| Claim | Verification |
|---|---|
| `pbatch/eval.py:164` — `run_suite` silently `continue`s on filter miss | ✅ `run_suite` at 164; `if only_id and case_id != only_id: continue` at 169–170 |
| `pbatch/eval.py:235` — `eval_main` CLI entry | ✅ `def eval_main(argv)` at 235 |
| `pbatch/eval.py:261` — `total += 1` only counts executed cases | ✅ inside the `for case_id, ok, detail in results` loop |
| `pbatch/eval.py:275` — `sys.exit(1)` unreachable when `failed` empty | ✅ `if failed:` at 273 → exit(1) at 275; empty → falls to 276 `"EVAL: all 0 passed"` → exit 0 |
| `tests/test_eval_suite.py:87` — crafted wrong-expectation `di-login` case | ✅ line 87; asserts exit 1 at 94 — run-and-fail filter is loud, only zero-match is silent |
| Live: `--filter no-such-case --json` | ✅ `{"total":0,"failed":0}` exit 0 (fail-open reproduced) |
| Live: `--filter system-type-state-machine --json` | ✅ `{"total":1,"failed":0,"failures":[]}` exit 0 |
| Live: no filter | ✅ `{"total":42,"failed":0,"failures":[]}` exit 0 |
| `make eval` = `python -m pbatch eval` | ✅ Makefile:41 |
| Exit-2 path prints no stdout JSON (F-7 baseline) | ✅ vacuous-case path only `log.error` (stderr, `config.py:19` basicConfig has no `stream=`); `test_vacuous_case_exits_2` asserts `"{" not in out` |

Root cause (three-line chain, confirmed): `continue` (eval.py:170) → `total` never increments (261) → `if failed:` empty (273) → fallthrough to `"all 0 passed"` (276) → exit 0.

## 2. Decision

Guard on the **executed-case count** in `eval_main`: after the run loop, before the JSON print, `total == 0` → `log.error` naming the filter → `sys.exit(2)`, **no JSON payload on stdout**. Placed after the `--coverage` branch so the coverage display path is untouched (requirements out-of-scope). The guard counts *executed* cases, so the `--quick` variant of the same hole (filter matches a case whose suite was skipped) is closed by the same rule — no separate `--quick` logic.

No changes to `run_suite` (its `continue` is correct; the caller must guard), no new CLI flags, no JSON schema change, no evals/*.yaml changes.

## 3. API changes

### 3.1 `pbatch/eval.py` — `eval_main` only (6-line surface, contained)

```python
    if args.coverage:
        _print_coverage(total, failed, args.json)
        return
    if total == 0:
        if args.filter:
            log.error("EVAL: --filter %r matched no eval cases (exact case id "
                      "required; see evals/*.yaml)", args.filter)
        else:
            log.error("EVAL: executed 0 eval cases (no evals/*.yaml loaded?)")
        sys.exit(2)
    if args.json:
        print(json.dumps({"total": total, "failed": len(failed),
                          "failures": failed}, ensure_ascii=False, indent=2))
    if failed:
        log.error("EVAL: %d/%d failed", len(failed), total)
        sys.exit(1)
    log.info("EVAL: all %d passed", total)
```

Behavior matrix (before → after):

| Invocation | Before | After |
|---|---|---|
| `pi-batch eval` | exit 0, total=42 | **unchanged** |
| `pi-batch eval --quick` | exit 0, total=35 | **unchanged** |
| `pi-batch eval --filter <existing id>` | exit 0/1 | **unchanged** |
| `pi-batch eval --filter <typo>` | exit 0, `{"total":0,"failed":0}` | **exit 2**, stderr names filter, **no stdout JSON** |
| `pi-batch eval --quick --filter <assess-case-id>` | exit 0, total=0 | **exit 2** (same guard; executed set empty) |
| `pi-batch eval --filter <typo> --coverage` | exit 0, coverage printed | **unchanged** (out of scope) |
| `pi-batch eval` with empty `evals/` (no yaml) | exit 0, "all 0 passed" | exit 2, "executed 0" |

### 3.2 Module docstring contract update

Extend the exit-code paragraph in `pbatch/eval.py` docstring:

```
Exit codes: 0 = all pass; 1 = assertion failures (CI-friendly); 2 =
schema/vacuous-case error OR --filter matched zero cases (consistent with
load_eval_files). Exit 2 never emits a JSON payload (F-7): a filter typo
is a usage error, not a run result — the message naming the filter goes to
stderr (logging), stdout stays clean.
```

### 3.3 Explicitly unchanged

- `run_suite(suite, only_id)` signature, semantics, and `continue` (exact-match filter semantics preserved: `case_id != only_id → skip`; partial-match filters like `--filter system-type-state` now fail closed instead of silently passing).
- JSON payload shape `{"total", "failed", "failures"}` on exit 0/1 — no new fields (compat for existing parsers, e.g. `test_entrypoints.py:61`).
- CLI surface: `--filter`, `--quick`, `--json`, `--coverage` — no additions/removals.
- `load_eval_files`, `VacuousCaseError` exit-2 path.
- `--coverage` display path, `--quick` suite selection, fuzzy matching, exit-code renumbering, sibling directions — all out of scope per requirements.

## 4. Compatibility constraints

- **Exit-code discrimination preserved**: 0 = ran+passed, 1 = ran+failed, 2 = executed nothing / usage error. CI that treats nonzero as failure now catches the typo (the point of the change); CI that previously relied on exit 0 + `total:0` for "no cases" is the broken pattern being removed.
- **stdout contract on exit 2**: empty stdout (identical to the pre-existing vacuous-case path). Parsers must already handle exit 2 + empty stdout; no new contract.
- **Verified unaffected consumers**:
  - `make eval` (Makefile:41): unfiltered → total=42, exit 0.
  - `tests/test_context_eval.py::test_eval_suite_passes` — unfiltered, exit 0.
  - `tests/test_context_eval.py::test_eval_single_case_filter` — matching filter `demo-login-loads-visual-core-only`, exit 0.
  - `tests/test_entrypoints.py:59` — unfiltered `--quick --json`, exit 0, payload shape unchanged.
  - `tests/test_eval_suite.py::test_filter_targets_renamed_cases` (AC2) — 10 matching filters, each `{"total":1,"failed":0,"failures":[]}`, exit 0 — **must pass unmodified**.
  - All unit tests in `test_eval_suite.py` use matching filters or no filter → no test currently exercises the zero-match path (design-stage note confirmed: no existing test uses a non-matching filter).
- **Message routing**: `log.error` → stderr (basicConfig default, `pbatch/config.py:19`); JSON → stdout. F-7 holds: exit 2 leaves stdout clean.
- **Duplicate case ids across suites**: a filter matching N>1 identical ids yields total=N — pre-existing semantics, unchanged (no de-dup added; out of scope).
- No config, data, or evals/*.yaml migration; no changes to `pi-batch.yaml` validators.

## 5. Failure modes

| Mode | After fix |
|---|---|
| Filter typo / no match | exit 2; stderr names filter; no stdout JSON |
| Partial-match filter (`--filter system-type-state` vs exact id) | exit 2 — exact-match semantics preserved, now loud instead of silent |
| Filter matches, case fails assertion | exit 1 with JSON payload (unchanged; already loud, pinned by `test_eval_suite.py:83-94`) |
| `--quick` + filter hitting a non-quick suite (e.g. assess case id) | exit 2 — one guard closes both holes (executed set empty) |
| Filter matches a vacuous case | exit 2 schema error naming suite/case (unchanged) |
| Empty `evals/` dir (no yaml files) | exit 2 "executed 0" (was silent pass — same bug class, closed) |
| `--coverage` + typo filter | exit 0 with coverage data (unchanged; display path out of scope) |
| Malformed yaml / id-less case | exit 2 at load (unchanged, already fail-closed) |
| Guard never triggers on valid runs | `total ≥ 1` always when filter matches or no filter with non-empty evals/; unfiltered run of an empty evals/ is the only new exit-2 case — intended |

## 6. Migration steps

1. Apply the guard in `eval_main` (Section 3.1) — sole code change.
2. Update the module docstring exit-code contract (Section 3.2).
3. Add regression tests (Section 7: AC1a, AC1b, FR4, edge).
4. Run gates, in order:
   - `python -m pytest tests/test_eval_suite.py tests/test_context_eval.py tests/test_entrypoints.py` (targeted)
   - `make eval` (AC3 — must stay exit 0, all 42 pass)
   - `make ci` (387 tests + 25 eval cases + harness)
   - `python quality.py --strict .`
5. No data migration, no config change, no `evals/*.yaml` edits (42 cases unchanged), no doc-tree edits outside this run's artifacts (historical `docs/auto/requirements/evals-85368cf1-requirements.md` documents the previous direction and is not updated).

## 7. Testable acceptance mapping

| # | Acceptance (testable) | Test (in `tests/test_eval_suite.py` unless noted) |
|---|---|---|
| AC1a | Unit: crafted suite + `--filter no-such` → `SystemExit` code 2, stdout contains no `{` (F-7), stderr/log names the filter | NEW `test_filter_no_match_exits_2` (patched EVAL_DIR; assert `exc.value.code == 2`, `"no-such" in caplog text`, `"{" not in capsys.readouterr().out`) |
| AC1b | Subprocess: real `evals/` + `python -m pbatch eval --filter no-such-case --json` → returncode 2, stdout has no JSON payload, stderr contains `no-such-case` | NEW subprocess regression test (read-only, mirrors `test_filter_targets_renamed_cases` harness) |
| AC2 | Every renamed case id targeted by `--filter` → exit 0, `{"total":1,"failed":0,"failures":[]}` | EXISTING `test_filter_targets_renamed_cases` (parametrized, 10 ids) — **must pass unmodified** |
| AC3 | `make eval` (unfiltered) → exit 0, all 42 pass | EXISTING `test_context_eval.py::test_eval_suite_passes` + Makefile:41 command body pinned |
| FR-4a | `--quick --filter <assess-case-id>` → exit 2 (executed set empty; one guard closes both holes) | NEW unit test (`--filter l3-prescription-no-billing-without-signal`) |
| FR-4b | `--quick --filter system-type-state-machine` → exit 0, total=1 | NEW unit test |
| FR-2 | Matching filter + wrong expectation → exit 1 **with** JSON payload (1 vs 2 discriminated) | EXISTING `test_eval_suite.py:83-94` (`di-login` wrong-expect) |
| F-7 | Exit-2 paths emit no stdout JSON (schema error + no-match) | EXISTING vacuous-case test + NEW AC1a/AC1b assertions |
| Edge | Empty `evals/` dir (no yaml) → exit 2, not "all 0 passed" | NEW unit test (patched EVAL_DIR with no files) |
| Negative | Unfiltered, non-empty suite → guard never fires | Covered by AC3 + `test_entrypoints.py:59` (unmodified) |

## 8. Verification of the design against requirements

- FR "fail-closed exit 2 with message naming the filter" → Section 3.1 guard; message `%r`-quotes the filter value.
- F-7 no-JSON consistency → guard placed before `if args.json:` print; stdout empty on exit 2, matching the existing vacuous-case path.
- Exact-match semantics preserved → `run_suite` untouched; only the caller now treats a zero-result set as an error.
- Guard on executed case set (one rule closes `--quick` variants) → guard reads `total` (executed count), so `--quick`-skipped suites yield the same exit 2 with zero extra code.
- AC1/AC2/AC3 preserved → Section 7 mapping; AC2 and AC3 require no test modification.
- Out-of-scope respected → coverage display path, `--quick` suite selection, fuzzy matching, exit-code renumbering, sibling directions (`--coverage masks failures`, nested-complexity) all untouched.
