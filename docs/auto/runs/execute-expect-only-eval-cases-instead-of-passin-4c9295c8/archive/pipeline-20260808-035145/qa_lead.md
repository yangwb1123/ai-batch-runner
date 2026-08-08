All verification complete. 755 passed (full sweep minus the three lock-flaky files), zero eval-related regressions. Here is my QA lead review.

---

# QA Lead Review — direction `85368cf1` (execute expect-only eval cases instead of passing them vacuously)

**Review basis**: design `docs/auto/design/evals-85368cf1-design.md` + implemented tree (`pbatch/eval.py`, `evals/*.yaml`, `tests/test_eval_suite.py`), reviewed against the live tree this revision. All commands below re-executed by me; no claim inherited from prior evidence without re-running.

## 1. Test inventory and commands actually run (this revision)

| Command | Result | Evidence |
|---|---|---|
| `python -m pbatch eval --json` | `total: 41, failed: 0`, rc 0 | ✅ Verified |
| `python -m pbatch eval --quick --json` | `total: 35, failed: 0`, rc 0 | ✅ Verified |
| `python -m pbatch eval` (text mode) | rc 0 | ✅ Verified |
| `--filter` × **all 10** renamed ids (6 system-type + 4 di/methodology) | each `total: 1, failed: 0`, rc 0 | ✅ Verified (superset of A1/A2) |
| `--filter does-not-exist-xyz --json` (F6) | `total: 0`, rc 0, silent no-op | ✅ Verified (pre-existing, documented) |
| `make eval` | rc 0 | ✅ Verified |
| `python -m pytest tests/test_eval_suite.py -q` | 5 passed | ✅ Verified |
| `python -m pytest tests/test_eval_suite.py --cov=pbatch.eval` | 5 passed; **line 67%** (48/147 missed) | ✅ Verified |
| Eval-adjacent subset (eval_suite/entrypoints/design_intelligence/rule_matcher/backend_experience/assessor) | **72 passed** | ✅ Verified |
| Full sweep `tests/` minus `test_pi_batch.py`/`test_local_fixes.py`/`test_agent_engineering.py` (lock-flaky) | **755 passed, 1 skipped** | ✅ Verified |
| `python quality.py pbatch/eval.py` | `quality: OK`, rc 0 | ✅ Verified — **F-1 High blocker fixed** |
| Cyclomatic per-function scan (ast) | `run_suite` 9, `eval_main` 10, all ≤ 15 budget | ✅ Verified |
| `python -m pbatch check` | **all 8 gates passed**, rc 0 (eval: all 41 passed) | ✅ Verified |
| Probes (temp EVAL_DIR via monkeypatch): `assert:{}` + executable expect → fallback executes (wrong expect → **rc 1**); `assert:{}` + display-only expect → **rc 2**; vacuous + `--coverage` → **rc 2, no coverage output**; vacuous `--json` → **rc 2, stdout 0 bytes** | all as stated | ✅ Verified |
| Manifest equivalence: `"…含支付审批，生产，go"` vs `"…含支付审批，生产环境，go"` | **identical** rule-id sets (8 di ids incl. the 6 expected), tier `production` | ✅ Verified |
| Data audit: `grep` all evals → **41/41 cases have `id:`**, 0 `name:`-keyed cases, 0 `assert: {}` in `evals/` | clean today | ✅ Verified |
| Lock state: `.pi-batch.lock` held by **live pid 1684832** (campaign) | full `make ci` not creditable in this tree | ✅ Verified (environmental) |

## 2. Requirement-to-test matrix

| Req | Status | Evidence (measured this revision) |
|---|---|---|
| R1 — expect-as-fallback when assert absent; assert wins | **Met** (doc contradiction, F-3) | `_effective_asserts` (`if asserts:` — `{}` is falsy → fallback); test 4 passes; probe rc 1/rc 2 both directions |
| R2 — `system_type`/`tier`/`includes`/`excludes` executable | **Met** | `_assert_value` dispatch live; tests 1–2; `tier` covered only via real-suite workbench case (Partial — no unit pin, F-6) |
| R3 — vacuous → exit 2 naming suite/case | **Met** | `VacuousCaseError`; test 3; E2E probe rc 2, empty stdout |
| R4 — 6 classifier `name:`→`id:` renames | **Met** | `evals/classifier.yaml` diff; all 6 filters target |
| R5 — 4 rules renames + 2 input corrections | **Met** | `evals/rules.yaml` diff; all 4 filters target; corrected workbench input now at `test_design_intelligence.py:112` (see F-5) |
| R6 — suite green (`eval`, `make eval`, entrypoints, `make ci`) | **⚠️ Not creditable in this tree** | `eval`/`make eval`/entrypoints/pytest all green; `make ci` blocked by live campaign lock (exit-5 flakes in 3 files) — pre-existing, not this change |
| R7 — new pytest coverage (3 mandated) | **Met (5 tests, superset)** | all pass; real `evals/` read-only (monkeypatched `EVAL_DIR`; subprocess test only reads) |
| A1/A2/A3/A4 | **Met (A1–A3 verified)**; A4's "make ci green" exception as above | all 10 filter ids verified; vacuous E2E verified |

## 3. Findings (severity-sorted)

**F-1 (was High) — `run_suite` complexity regression: ✅ FIXED.** Verified at three levels: `quality.py pbatch/eval.py` → OK; ast cyclomatic `run_suite` = 9 (was 18), `eval_main` = 10; `pi-batch check` → all 8 gates rc 0. The `_effective_asserts`/`_list_assertion_failure` extraction resolved it. Acceptance assertion: `python quality.py .` → OK **and** `python -m pbatch eval --json` → 41/0 rc 0 — **both green now**. Closed.

**F-2 (Medium) — Atomic-commit dependency (open, procedural).** `test_filter_targets_renamed_cases` subprocess pins real `evals/` data; a commit landing machinery without data renames is silently vacuous-green at `total: 41` (the exact defect this direction fixes). Tree still carries machinery+data+tests uncommitted together — good. **Exact control**: single commit containing `pbatch/eval.py` + `evals/*.yaml` + `tests/test_eval_suite.py`; state it in the commit message. No code change needed.

**F-3 (Low) — `assert: {}` contract: doc contradicts code, behavior unpinned (open).** Verified live: `assert: {}` + executable expect → expect **executes** (probe rc 1); `assert: {}` + display-only → rc 2 (vacuous). Design doc §2.3 line 161–162 ("even `assert: {}` it wins and expect stays inert") and §4 F5 line 292 ("`assert: {}` → vacuous exit 2") **both false** for the first case. **Exact test to add** (in `tests/test_eval_suite.py`): `test_assert_empty_dict_uses_expect_fallback` — `assert: {}` + deliberately-wrong `expect.system_type` on state-machine input → `SystemExit(1)` (fallback ran), plus `assert: {}` + display-only → `SystemExit(2)`. Also correct doc lines 161–162/292. **Acceptance**: both sub-cases produce pinned exit codes.

**F-4 (Low) — `--coverage` claim false in design §3 line 271 ("does not call `run_suite`" → "never triggers exit 2") (open, doc-only).** Verified live: vacuous case + `--coverage` → **rc 2, no coverage payload** — `eval_main` runs the loop before the coverage branch (correct fail-closed direction; doc wrong). Fix the doc sentence; a unit pin is optional (behavior is the safer contract).

**F-5 (Low) — Cross-pinning attribution wrong; backend input not pytest-pinned as a manifest assertion (open, doc-only + optional test).** Actual: workbench string at `test_design_intelligence.py:112` (doc cites `:96/:106`); the eval backend input appears only in `tests/test_eval_suite.py:133` as an `assert.task_type` input — **no pytest test asserts its 6 di-id manifest**. I verified live that `"…生产，go"` (pinned at `test_backend_experience.py:97`) and `"…生产环境，go"` (eval) produce **identical** manifests (8 di ids incl. all 6 expected, tier production) — behavioral risk nil. Fix doc citations; optional cheap test: one read-only `match_rules` assertion on the eval backend string.

**F-6 (Low) — Rename layer pinned 1-of-10; no id-schema guard (open — highest-value remaining gap).** Only `system-type-state-machine` is pytest-pinned (`test_filter_targets_renamed_cases`); the other 9 ids are guarded only by manual A2 commands. Worse: nothing fails closed on a future id-less/`name:`-keyed case — `run_suite` defaults to `"?"` and `eval --json` stays `41/0` while the case silently becomes untargetable (the exact defect this direction fixes). My data audit shows all 41 cases have `id:` **today**, so no live risk. **Exact tests to add**: (1) parametrize `--filter` over all 10 renamed ids → `total == 1` each; (2) a suite-scan test asserting **every** case in the monkeypatched EVAL_DIR has `id:` (fail-closed, mirroring the vacuous guard), or add the check to `load_eval_files` (exit 2) for fail-closed enforcement. **Acceptance**: parametrized test passes; a crafted id-less case → exit 2 (or test-level rejection).

**F-7 (Info) — Exit-2 no-JSON asymmetry verified but unpinned (open, cheap).** Live probe: vacuous `--json` → rc 2, **stdout 0 bytes** (exit 1 prints a payload). `test_vacuous_case_exits_2` asserts only rc + log text. Add `assert "{" not in out` to that test.

**F-8 (Info) — Branch gaps (open, optional).** `expect.tier` fallback unit-tested only implicitly via real-suite workbench case; `--quick`/`--coverage`/text paths unpinned at unit level (67% line coverage — behavioral adequacy high, but one `expect: {tier: production}` unit test is cheap). No security, credential, tenant-boundary, replay/rotation, or recovery paths implicated: this is a pure test-harness behavior change with one documented exit-code/JSON contract, verified end-to-end.

## 4. Prioritized scenario list (execution status)

| # | Scenario | Path | Status |
|---|---|---|---|
| S1 | Happy: formerly-vacuous case executes real assertion, passes | system-type ×6, di ×4 | ✅ measured (41/0) |
| S2 | Happy: `--filter` targets renamed case, exact total 1 | **all 10 ids** | ✅ measured |
| S3 | Error: vacuous case (display-only expect) → rc 2, names suite/case, **no JSON payload** | A3 + probe | ✅ measured (rc 2, 0 stdout bytes) |
| S4 | Error: wrong expectation → rc 1 with `{total, failed, failures}` | tests 1–2 | ✅ measured |
| S5 | Boundary: `assert` present wins over wrong `expect` | test 4 | ✅ measured |
| S6 | Boundary: `assert: {}` + executable expect → fallback executes; + display-only → rc 2 | probes | ✅ measured live, **⚠️ unpinned** (F-3) |
| S7 | Boundary: scalar (non-list) `includes` | `expected_list` wrapper | ⚠️ untested (low risk, pre-existing) |
| S8 | Error: unknown expect-key typo (e.g. `includesz`) silently dropped as "display" | expect filter | ⚠️ untested — fail-closed philosophy doesn't extend to expect-key typos (pre-existing asymmetry vs `assert` unknown key → loud FAIL); consider a warning (out of scope) |
| S9 | Error: `--filter <typo>` → total 0 rc 0 | F6 | ✅ measured (pre-existing, documented) |
| S10 | Recovery: exit 2 mid-run discards other suites' results, no payload | eval_main | ✅ measured (fail-closed; acceptable, noted) |
| S11 | `--quick` (35) / `--coverage` (41) / text mode | — | ✅ measured; `--coverage`+vacuous → rc 2 (F-4) |
| S12 | Regression: keyword drift re-routes corrected inputs | — | ✅ double-pinned (eval + `test_design_intelligence.py:112`) except backend input (F-5 — verified identical manifests) |

Race/duplicate/partial-success scenarios: N/A — pure CLI regression harness, no concurrency/IO in scope (checked, not skipped).

## 5. CI/manual-suite gaps, flake risks, exit criteria

- **CI gap (environmental, blocking credit of A4 only)**: `.pi-batch.lock` held by live pid 1684832 (campaign). Full `make ci` will fail exit-5 in `test_pi_batch.py`/`test_local_fixes.py`/`test_agent_engineering.py::test_check_subcommand_passes` — failure set previously verified identical with changes stashed (backend_engineer: 19/166; qa_lead: 25/958). **Not caused by this change**; re-run post-campaign-merge with no concurrent pi-batch holding the lock.
- **Flake risk (design)**: `test_filter_targets_renamed_cases` couples to real `evals/` data + cwd — correct (it's acceptance A1) but demands atomic commit (F-2).
- **Fixtures needed**: none — existing monkeypatched `EVAL_DIR` + `tmp_path` pattern suffices; all real-data reads read-only (verified; tests write only to `tmp_path`).
- **Doc fixes before commit (cheap, required for honesty)**: design §2.3/F5 (`assert: {}`), §3 coverage claim, §2.6 pin attribution (`:112`, backend input not pinned) — F-3/F-4/F-5.
- **Exit criteria**:
  1. `python quality.py .` → OK — ✅ **already green**
  2. `python -m pbatch check` → rc 0, all 8 gates — ✅ **already green**
  3. `python -m pbatch eval --json` → 41/0 rc 0; A1/A2 filters → 1/0 each — ✅ **already green**
  4. `python -m pytest tests/test_eval_suite.py tests/test_entrypoints.py -q` → green — ✅ (72 incl. adjacent)
  5. **F-3 pin test added and passing; design-doc claims corrected — ⚠️ open**
  6. **F-6 parametrized 10-id filter pin (+ id-schema guard preferred) — ⚠️ open**
  7. F-7 one-line no-JSON assertion in test 3 — ⚠️ open (cheap)
  8. `make ci` full green post-campaign-merge, committed atomically (machinery + data + tests in one commit) — ⚠️ pending environment

**Bottom line**: the direction's core defect is fixed and proven — all 10 formerly-vacuous cases execute real assertions (41/0), all 10 renamed ids are filter-targetable, fail-closed exit-2 semantics verified end-to-end, the previous High blocker (quality gate) is **closed and re-verified at three levels**, and 755 non-lock tests pass with zero eval-adjacent regressions. Remaining items are one Medium procedural control (atomic commit) and four Low/Info test-pin + doc-accuracy fixes (F-3/F-5/F-6/F-7) — F-6 (parametrized id pin + id-schema guard) is the one I recommend before merge to make the rename layer regression-proof, since `eval --json`'s `total: 41` is structurally blind to a name-keyed regression. No security, tenant, credential, or recovery concerns apply to this change.
