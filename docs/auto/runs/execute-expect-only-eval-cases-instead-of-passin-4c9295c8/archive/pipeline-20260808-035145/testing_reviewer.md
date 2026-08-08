Verification complete. The tree was restored after the reproduce-first check (41/0 re-confirmed post-restore). Here is my review.

---

# Testing Review — direction `85368cf1` (execute expect-only eval cases instead of passing vacuously)

**Evidence state**: design deliverable + implementation both landed in the working tree (with 4+ concurrent campaigns' uncommitted work present). I reviewed the **implemented state** and re-ran every acceptance command; the reproduce-first check was done by checking out HEAD `pbatch/eval.py` + `evals/*.yaml` into the tree, running the tests, and restoring (41/0 re-verified after restore).

## 1. Verdict

```
VERDICT: PASS - core defect fixed and proven: 41/41 real assertions (rc 0), all
5 renamed filter ids targetable (total 1 each), 4/5 new regression tests fail on
pre-change code, vacuous case -> exit 2 with no JSON payload (fail closed).
The qa_lead High blocker (run_suite complexity 18>15 breaking quality/make ci)
is RESOLVED in the shipped code (_effective_asserts + _list_assertion_failure
extraction): quality.py OK, `pi-batch check` all 8 gates rc 0. No blocking test
gaps. Non-blocking: 4 findings — rename layer pinned 1-of-10 (fix before merge),
assert:{} edge unpinned + doc contradicts code, --coverage contract claim false,
exit-2 payload asymmetry unpinned.
```

**Executed live**: `eval --json` → 41/0 rc 0 · all 5 `--filter` ids → total 1/0 each · `--quick` → 35/0 · `make eval` rc 0 · `pytest test_eval_suite.py test_entrypoints.py` → 11/11 · eval-adjacent subset (eval_suite, entrypoints, design_intelligence, classifier, rule_matcher, assessor, backend_experience) → **104/104** · `quality.py pbatch/eval.py` → OK · `pi-batch check` → 8/8 gates rc 0 · pre-change checkout → **4 of 5 new tests fail** · probes P1–P4 (below) · migration diff audited.

## 2. Findings table

| Sev | Defect pattern | Missing test / doc error | Exact test that catches it |
|---|---|---|---|
| **Med** | Rename layer pinned 1-of-10; `eval --json` is blind to a rename regression (a `name:`-keyed case still executes its assertions, reports as `?`, total stays 41 — the untargetability half of the original defect returns silently). No schema guard rejects id-less cases (`load_eval_files` checks only `cases` list; `run_suite` defaults `id` to `"?"`). | Only `test_filter_targets_renamed_cases` pins one id; A2's other 4 filter commands are manual acceptance. | Parametrize over all 10 renamed ids: `eval --filter <id> --json` → `total == 1` for each; plus a read-only suite-scan test asserting **every** `evals/*.yaml` case has a non-empty `id:` (fail closed, mirroring the vacuous guard). |
| **Med** | `assert: {}` contract contradiction. Design §2.3 "even `assert: {}` — it wins" and §4 F5 "`assert: {}` → vacuous exit 2" — **both false vs shipped code**: `_effective_asserts` treats `{}` as absent → expect fallback executes (probe P1: `assert: {}` + correct `expect.system_type` → PASS rc 0). Module docstring is correct ("absent (or `{}`)") — the design doc is the liar. | No test pins the edge. | `assert: {}` + a deliberately **wrong** executable expect → exit 1 (proves fallback executes); `assert: {}` + display-only expect → exit 2. Then fix the two design-doc sentences. |
| **Low-Med** | `--coverage` contract claim false. Design §3: "coverage mode does not call run_suite, so it never triggers exit 2". Code runs every suite's `run_suite` before the coverage branch. | Probe P3: vacuous case + `--coverage` → **exit 2, no coverage payload**. Fail-closed behavior is defensible; documented contract is wrong and unpinned. | Test: vacuous suite + `eval --coverage` → `SystemExit(2)` (pin whichever contract is intended; today's is fail-closed). |
| **Low** | Cross-pinning citations wrong. Design §0: both inputs "byte-identical to strings pinned in `test_design_intelligence.py:96/106`". Actual: workbench input is at **:112** (verified), and the backend input `"…含支付审批，生产环境，go"` is pinned **nowhere** in pytest — nearest pins are `test_backend_experience.py:99` (`"…生产，go"`) and `test_agent_engineering.py:191` (`"…生产环境，go 实现"`), both different strings. Practical protection now exists (the eval case itself pins the input), so doc fix only. | — | No test needed; correct the design-doc attribution (and note eval+pytest now double-pin the workbench input only). |
| **Info** | Exit-2 no-JSON asymmetry unpinned. Probe P2 verified rc 2 with **empty stdout**; test 3 asserts only exit code + log text. | If a consumer parses stdout without checking rc, exit 2's empty stdout is the trap. | In `test_vacuous_case_exits_2`, add `assert "{" not in captured_stdout`. |
| **Info** | `expect.tier` fallback (workbench case) has no **unit** pin — covered indirectly: `test_entrypoints.py::test_eval_quick_mode_runs_core_domains` runs `--quick` (includes rules suite) and asserts `failed == 0`, so a tier-fallback break would fail that test. Acceptable; a unit test with `expect: {tier: production}` would close the gap. | — | Optional unit case as described. |

## 3. Risk-coverage matrix

| Risky path | Covered? | Evidence |
|---|---|---|
| Vacuous case silent-PASS (core defect) | ✅ | Typed `VacuousCaseError` → exit 2 naming suite+case; probe P2 (rc 2, empty stdout); test 3; **reproduce-first proven** (fails pre-change) |
| `system_type` dispatch | ✅ | test 1 (pass + wrong-expect FAIL → exit 1); fails pre-change |
| `includes`/`excludes` set semantics | ✅ | test 2 (pass + missing-id FAIL → exit 1); fails pre-change |
| `assert` precedence (expect inert) | ✅ | test 4 (wrong expect + real assert → PASS) — pins pre-existing behavior, correctly passes pre-change too |
| `assert: {}` edge | ❌ | **Finding 2** — behavior exists (P1), unpinned, design doc contradicts code |
| Filter targeting after rename | ⚠️ 1/10 | **Finding 1** — 9 ids verified live (all total 1) but not regression-pinned; id-less case would execute as `?` with no schema guard |
| Exit-code contract 0/1/2 | ✅ | 0 (all commands), 1 (tests 1–2, probe P4 with JSON payload), 2 (test 3, probe P2) |
| Exit-2 no-JSON asymmetry | ⚠️ | **Finding 5** — verified live (P2), not pinned |
| `--quick` (35, rules+classifier only) | ✅ | entrypoints `failed == 0, total >= 20`; live 35/0 |
| `--coverage` + vacuous case | ❌ | **Finding 3** — design claim false, unpinned (P3: exit 2) |
| Keyword/registry drift re-routing (F2/F3) | ✅ | Loud exit 1 by construction; workbench input co-pinned at `test_design_intelligence.py:112` |
| `--filter` typo silent no-op (F6) | ⚠️ | Pre-existing, documented out-of-scope; verified live (total 0, rc 0) |
| Races/timeouts/duplicates/permissions/partial-success | N/A | Pure CLI regression harness — no concurrency, IO, or timing surface; checklist items not applicable, not a gap |

## 4. Honesty audit

**Executed (not just claimed):**

| Claimed | Executed | Result |
|---|---|---|
| `eval --json` → 41/0, rc 0 | ✅ ran | `total: 41, failed: 0`, rc 0 |
| 5 × `--filter` → total 1 | ✅ ran all 5 | all `total: 1, failed: 0` |
| `make eval` green | ✅ ran | rc 0 |
| 5 new pytest tests | ✅ ran | 5/5 pass; 4/5 **fail on pre-change code** (verified via HEAD checkout + restore) |
| quick 35 unchanged | ✅ ran | 35/0 |
| eval-adjacent subset green | ✅ ran | 104/104 |
| quality gate (qa_lead's High blocker) | ✅ ran | `quality.py pbatch/eval.py` → **OK**; `pi-batch check` → 8/8 gates, rc 0 — blocker resolved in shipped code |
| `make ci` full green (R6) | ⚠️ **not re-run** | Tree carries 4+ concurrent campaigns' uncommitted changes (cli.py, config.py, campaign_*.py, lock.py, …). Prior reviewers observed 19–25 unrelated failures from lock contention/other campaigns. R6 **cannot be credited in this tree**; must be re-run post-merge of all campaigns. `pi-batch check` (the eval-adjacent gate stack) is green, which is the strongest available signal. |
| backend_engineer's "make ci → 939 passed" | ⚠️ misleading phrasing | 939 passed ≠ green (rc would be non-zero with 19 failures); he does acknowledge "could not fully run green" in the same summary — flag for reporting discipline, not fabrication. |

**Doc-accuracy gaps in the design deliverable (incorrect citations, not fabrication):**
- ":96/106 pinned strings" — workbench actually at `test_design_intelligence.py:112`; backend input pinned nowhere (Finding 4).
- "coverage never triggers exit 2" — false (Finding 3, probe P3).
- "`assert: {}` it wins" / "`assert: {}` → exit 2" — both contradicted by shipped code (Finding 2, probe P1).

**Positive honesty signals**: `--filter` typo no-op, exit-2 asymmetry, and doc-count drift are correctly marked pre-existing/out-of-scope rather than claimed as fixes; `evals/rules.yaml`'s new `backend-manifest-excludes-repo-internal-policy` case belongs to the concurrent godfile-split campaign (confirmed in diff, not this direction); migration diff is exactly the claimed 10 renames + 2 input corrections.

**Test engineering quality (positive)**: reproduce-first holds and is now *proven* (4/5 fail on HEAD — not just asserted); business-result assertions (exact JSON payload + rc) with no existence-only checks; isolation clean (monkeypatched `EVAL_DIR` + `tmp_path`, real `evals/` read-only via subprocess, no sleeps, no shared state); layer choice appropriate (unit dispatch + contract exit-codes + E2E subprocess + entrypoint E2E). Property/fuzz not applicable — no parsers or money allocation in scope.

**Merge gate**: fix Finding 1 (parametrized 10-id pin + id-presence schema guard) and correct the three design-doc claims (Findings 2–4) before/with commit; the data, machinery, and tests must land in one atomic commit (the entrypoint subprocess test fails otherwise — F-2 from the earlier qa review).
