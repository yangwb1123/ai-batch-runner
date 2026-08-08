Everything verified. Final state is coherent — no commit made. Here is the change report.

---

# Change Report — direction `85368cf1`: execute expect-only eval cases instead of passing them vacuously

## Assessment (per protocol)

`pi-batch assess "<direction title> <problem>"` → **L0_direct workflow** (分 0), 画像 unknown/standard → 处方档 **demo, 0 条规则**, 克制原则「只实现需求本身」. No persistence-design gate applies — this is a pure test-harness CLI change with no core data models.

## Files changed (all uncommitted; machinery + data + tests form one atomic change per F-2)

| File | Change |
|---|---|
| `pbatch/eval.py` | `EXECUTABLE_EXPECT_KEYS = (system_type, tier, includes, excludes)`; `VacuousCaseError`; `_effective_asserts` (expect-as-fallback, assert-wins for non-empty assert, `{}` treated as absent); `_assert_value` dispatch for `system_type` + rule-id set semantics for `includes`/`excludes` via `_list_assertion_failure`; `eval_main` → exit 2 on vacuous; docstring exit-code contract. **My session additions:** case-level schema guard in `load_eval_files` (case must be dict, non-empty `id:`, `assert`/`expect` mapping-or-absent → exit 2 naming suite+case); `_print_coverage(total, failed, json_out)` receives parsed `args.json` instead of scanning `sys.argv` |
| `evals/classifier.yaml` | 6 × `name:` → `id:` renames (system-type-*) |
| `evals/rules.yaml` | 4 × `id:` renames + 2 input corrections (workbench → `"企业订单管理工作台，含 KPI 与趋势图表，生产环境"`; backend → `"…含支付审批，生产环境，go"`), expectations byte-identical. (`backend-manifest-excludes-repo-internal-policy` belongs to the concurrent godfile-split campaign — untouched.) |
| `tests/test_eval_suite.py` | 17 tests: 5 original (system_type dispatch, includes/excludes dispatch, vacuous→exit 2, assert-wins, real-suite filter) + **my session additions:** parametrized `--filter` over all 10 renamed ids (total == 1 each); `test_assert_empty_dict_uses_expect_fallback` (F-3); `test_load_eval_files_rejects_id_less_case` / `test_load_eval_files_rejects_malformed_assert` (F-6 schema guard); F-7 `assert "{" not in stdout` in the vacuous test |
| `docs/auto/design/evals-85368cf1-design.md` | Corrected 5 claims contradicted by shipped code (F-3 ×2: `assert: {}` precedence + F5 table; F-4: `--coverage` exit-2; F-5 ×2: pin citations → `test_design_intelligence.py:112` / `test_backend_experience.py:99`) |
| `docs/auto/requirements/evals-85368cf1-requirements.md` | Same citation correction (F-5) |

## Command results (all re-executed this session)

| Command | Result |
|---|---|
| `python -m pbatch eval --json` / text / `--quick --json` | **41/0 rc 0** / rc 0 / **35/0 rc 0** |
| `make eval` | rc 0 |
| `--filter` × all 10 renamed ids | each `total: 1, failed: 0`, rc 0 |
| `pytest tests/test_eval_suite.py` | **17 passed** |
| `+ test_entrypoints.py + test_context_eval.py` | 34 passed |
| `+ design_intelligence + rule_matcher + backend_experience + assessor` | 61 passed |
| Full sweep `tests/` minus 3 lock-flaky files | **767 passed, 1 skipped**, rc 0 |
| `make check` / `self-test` / `org` / `quality` | all rc 0 |
| `python quality.py --strict .` | OK (F-1 complexity blocker stays resolved: `run_suite` 9, `eval_main` 10 ≤ 15) |
| `python -m pbatch check` | all **8 gates passed**, rc 0 |
| Probes (temp EVAL_DIR): vacuous `--json` → rc 2, **0 stdout bytes**, error names suite/case; vacuous `--coverage` → rc 2, no payload; id-less case → rc 2, no traceback; list-`assert` → rc 2, no traceback | all as stated |
| Reproduce-first: HEAD `pbatch/eval.py` + working data → **6 new tests fail** (schema guards, F-3, F-7, vacuous); restored → 17 pass | confirmed |
| `make ci` full | **Not creditable — environmental**: `.pi-batch.lock` held by live pid 1684832 (this campaign's own orchestration); lock-dependent files (`test_pi_batch.py`, `test_local_fixes.py`, `test_agent_engineering.py::test_check_subcommand_passes`) fail exit-5 with a failure set verified identical with changes stashed by two reviewers. Re-run post-campaign-merge. |

## Evidence-backed deviations from the design (reviewer-mandated merge hardening)

1. **`load_eval_files` case-level schema guard** (qa_lead F-6, architecture reviewer F2, testing_reviewer merge gate): a future `name:`-keyed/id-less case would otherwise silently report as `?` and re-introduce the exact untargetability defect this direction fixes; malformed `assert` previously leaked an `AttributeError` as rc 1. Now fail-closed exit 2 — consistent with `load_eval_files`' existing exit-2 schema contract. Design doc §2.2 did not specify it.
2. **`_print_coverage` argv fix** (architecture reviewer Low): programmatic `eval_main(["--coverage", "--json"])` now formats by the passed args, not the process argv.
3. **Design/requirements doc corrections** (F-3/F-4/F-5): the design's `assert: {}`-wins claim, its `--coverage` never-exits-2 claim, and its `:96/:106` pin citations were all false vs shipped code (verified by three reviewers + gate probes); corrected to match code and actual pin locations.
4. **Test count 5 → 17** (superset of R7's mandated 3): parametrized 10-id pin, F-3 pin, 2 schema-guard pins, F-7 assertion merged into the vacuous test.

## Not done

- No commit (per instruction). Tree also carries 4+ concurrent campaigns' uncommitted work; the atomic commit (machinery + `evals/*.yaml` + tests in one commit, per F-2) is left to the campaign flow.
- Out of scope (per design §7): classifier/rule_matcher routing changes, doc-count sync (25→41), `--filter` typo silent no-op (pre-existing, documented).
