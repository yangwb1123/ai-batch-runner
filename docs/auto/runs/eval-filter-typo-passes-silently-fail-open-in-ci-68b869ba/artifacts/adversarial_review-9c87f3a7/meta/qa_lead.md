# QA Lead Review — `eval --filter` no-match fails closed (exit 2)

Reviewing: design-606312fc/design.md + the implementation already present in the
working tree (`pbatch/eval.py` guard at 273–285, docstring contract updated) and
its tests (`tests/test_eval_suite.py:129–215`).

Method: every design claim re-verified live (code, tests, CLI); reproduce-first
proven by experiment (guard removed → new tests fail → restored); gates run as
claimed.

## 1. Verdict

**VERDICT: FAIL - blocking gap: `python quality.py --strict .` fails with the
guard as implemented (`eval_main` 54 lines > 50 budget) — the design's own
migration step 4 / repo hard gate is not met; fix is one edit (trim the 5-line
comment or extract a helper), all tests and behavior otherwise pass.**

## 2. Findings table

| # | Severity | Defect pattern | Evidence / impact | Exact test that catches it |
|---|---|---|---|---|
| F1 | **High** | Hard-gate violation (function-length budget) | `python quality.py --strict .` → `pbatch/eval.py:238 func 'eval_main' 54 lines > 50 budget`. Pre-guard version measures **OK** — the 11-line guard block (5-line comment + 6 statements) pushed it over. Design §6 step 4 claims this gate passes; it does not. AGENTS.md §4 mandates it for tool changes. | No test — the gate itself: `python quality.py --strict .` must report 0 violations. Fix: shrink comment to 1 line (→ 50 = at budget) or extract `_guard_zero_executed(total, args)`. |
| F2 | Medium | Same bug class remains reachable via a documented flag combination (fail-open) | Live: `python -m pbatch eval --coverage --filter no-such-case --json` → `{"total":0,"failed":0}` **exit 0**. Documented out-of-scope in requirements/design, and verified no CI consumer (`Makefile`, `scripts/`, `checks/` — `--coverage` is display-only), so Low operational risk — but a reviewer note: moving the guard above the `--coverage` branch (one line, changes only the zero-total case) closes it with zero extra code. | NEW unit test: `eval_mod.eval_main(["--coverage", "--filter", "no-such", "--json"])` → assert `SystemExit` code 2 and `"{" not in capsys.out` (deterministic, no timing). Currently exits 0 — the test documents the open hole. |
| F3 | Low | Partial-match semantics not pinned by a dedicated case | AC1b uses a wholly non-existent id (`no-such-case`); partial-prefix (`--filter system-type-state` vs real id `system-type-state-machine`) is structurally the same zero-match path, so risk is low — but the "exact case id required" contract (design §5, row 2) has no direct test. | NEW subprocess test: `--filter system-type-state` against real `evals/` → returncode 2, stderr contains the filter. (Passes today; locks exact-match semantics.) |
| F4 | Info | Duplicate-case-id semantics unpinned | Verified live: all 42 case ids in `evals/` are **unique** — the design's "duplicate id → total=N" note is theoretical. AC2's parametrization pins `total==1` for 10 renamed ids; the other 32 ids have no filter test. A cheap property: every real id filters to `total>=1`. | NEW in-process parametrized test over all 42 ids: `eval_main(["--filter", case_id, "--json"])` → exit 0, `total >= 1` (no subprocess needed; deterministic). |
| F5 | Info | FR-4a error message slightly misleading | `--quick --filter <assess-case-id>` exits 2 with "matched no eval cases (exact case id required)" — the case id *exists*, its suite was skipped by `--quick`. Correct behavior, suboptimal debuggability. | No test change; suggest message "matched no eval cases under --quick (exact case id required)". Non-blocking. |

## 3. Risk-coverage matrix (risky path × coverage)

| Risky path | Covered? | Test / evidence |
|---|---|---|
| Filter typo / zero-match → exit 2 | ✅ | AC1a unit (`test_filter_no_match_exits_2`), AC1b subprocess (`test_filter_no_match_subprocess_exits_2`) |
| Partial-match filter (exact-match preserved, now loud) | ✅ (structurally) / ⚠️ no dedicated test | Same zero-match path; F3 proposes the pin |
| `--quick` + filter hitting skipped suite | ✅ | FR-4a `test_quick_filter_hitting_skipped_suite_exits_2` |
| `--quick` + filter matching core case (positive control) | ✅ | FR-4b `test_quick_filter_matching_core_case_still_passes` |
| Matching filter + failing case → exit 1 **with** JSON (0/1/2 discrimination) | ✅ | Pre-existing pin `test_eval_suite.py` wrong-expect `di-login` (exit 1) |
| Exit-2 stdout clean (F-7), message on stderr naming filter | ✅ | `"{" not in out` + caplog/stderr assertions in AC1a, AC1b, Edge |
| Empty `evals/` dir → exit 2 (same bug class) | ✅ | `test_empty_eval_dir_exits_2` |
| Vacuous case + filter → exit 2 | ✅ | Pre-existing `test_vacuous_case_exits_2` (unfiltered); filtered path hits same `VacuousCaseError` → exit 2 |
| Unfiltered run unaffected (exit 0, 42 cases) | ✅ | AC3 `test_context_eval.py::test_eval_suite_passes` + live `make eval` body (42/0) |
| `--quick` unfiltered unaffected (35 cases) | ✅ | `test_entrypoints.py` quick-mode test (35 ≥ 20) |
| `--coverage` + zero-match | ❌ **Open** | F2 — live-verified fail-open `{"total":0}` exit 0 |
| Duplicate case ids across suites | ⚠️ N/A today (42 unique, verified) | F4 — optional property test |
| Concurrency/races/timeouts/permissions/partial-success | N/A | Deterministic synchronous CLI; no timing surface exists — checklist items structurally inapplicable, not skipped |
| Sleep-based timing control | N/A (none used) | All tests deterministic; `timeout=120` only bounds subprocess harness |

## 4. Honesty audit (completion-evidence spec)

| Claim | Result |
|---|---|
| All 7 design evidence rows re-verified live (line numbers, `continue` at 169–170, `total += 1`, `if failed:` fallthrough, Makefile:41, basicConfig no `stream=` → stderr) | ✅ Verified, no line shifts |
| Live fail-open repro (exit 0, `{"total":0}`) | ✅ Confirmed by experiment: guard removed → 4/5 new tests fail (`DID NOT RAISE`, returncode-2 assertion fails) → the tests genuinely reproduce the defect; **defect cannot return unnoticed** |
| `--filter system-type-state-machine` → total=1; no filter → 42; `--quick` → 35 | ✅ Verified live; also confirmed `rules.yaml:106` occurrence is an `includes:` **rule id**, not a duplicate case id |
| AC2 `test_filter_targets_renamed_cases` (10 ids, `total==1`) passes unmodified | ✅ Verified |
| Targeted gate `pytest tests/test_eval_suite.py tests/test_context_eval.py tests/test_entrypoints.py` | ✅ **39 passed** (executed) |
| `make eval` stays exit 0 / 42 | ✅ Verified live (AC3) |
| `quality.py --strict .` passes (design §6 step 4) | ❌ **FAILS** — 1 violation (`eval_main` 54 > 50) — F1, must be fixed |
| `make ci` (387 tests + 25 eval + harness) | ⚠️ not_executed in this review — implementation-stage gate; targeted 39 passed, changed surface contained (6-line guard + docstring + 5 tests), so risk is bounded |
| "6-line surface" claim in design §2/§3 | ⚠️ Partial — functional surface is 6 statements, but the realized block is 11 lines with comment; the discrepancy is exactly what broke the quality gate |
| No skipped tests, no "理论上应该通过" phrasing, no expectation edits | ✅ Verified — all new tests assert real exit codes/payloads; FR-4b correctly passes with and without the guard (positive control) |

## 5. Assessment against testing spec

- **Five layers**: unit (patched `EVAL_DIR` fixture) + integration (subprocess vs real `evals/`) + contract (JSON payload shape, exit-code discrimination, stdout/stderr routing pinned) + E2E (`make eval` body, unfiltered suite) — appropriate layering for a CLI exit-code change; non-functional N/A (no perf/concurrency surface).
- **Reproduce-first**: ✅ demonstrated operationally (see honesty audit) — the tests fail on the pre-fix code and pass on the fixed code.
- **Assertion quality**: ✅ exit codes, exact payload dicts, stdout-clean and stderr-naming asserted; no exists-only checks.
- **Isolation**: ✅ `tmp_path` + `monkeypatch` per test, no shared mutable state, no execution-order dependence, no sleeps.
- **Data governance**: ✅ crafted 6-line suites express scenario semantics (no giant fixtures).
- **Risk coverage**: ✅ for the executed-count semantics (0/1/2 discrimination, `--quick` variants, empty dir, F-7); ❌ one documented residual (`--coverage` zero-match, F2).

**Bottom line**: the fix logic, tests, and honesty are strong — reproduce-first is proven, coverage is risk-shaped, and every evidence claim held up under live re-verification. One blocking item stands between this and done: the function-length budget gate fails (F1), a one-edit fix. F2 (coverage hole) is a documented, deliberate residual with no CI consumer; recommend closing it with a one-line guard move while touching the file anyway.
