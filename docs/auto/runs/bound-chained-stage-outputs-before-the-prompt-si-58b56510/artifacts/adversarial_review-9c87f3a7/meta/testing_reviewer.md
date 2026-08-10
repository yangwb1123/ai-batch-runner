# QA Lead Review — Bound Chained Stage Outputs (`ai/run-review.py`)

**Verdict line: `VERDICT: FAIL - blocking test gaps: the implementation and all three planned tests (T1/T2/T3) do not exist in the repository; the reproduced defect (stages 06/07/09 reject at 30 KiB chained outputs) remains fully unfixed, and the existing 21-test suite passes only because nothing changed.`**

## What I actually executed (this review)

| Check | Command / method | Result |
|---|---|---|
| Test suite | `python -m pytest tests/test_run_review.py -q` | 21 passed — all exercise *old* behavior |
| Defect reproduction | importlib-loaded live `chain_variables`, 9 sources × 30 KiB (stage 09 shape) | **276,712 chained bytes > 122,880 gate → `_prompt_size_error` fires** |
| Implementation presence | grep `_chain_budget` / `_truncate_text` / `_fair_allowances` / kwarg signature across tree, stashes, branches | **Absent** — `chain_variables` at :373 is the old 4-positional-arg, unbounded version; `_stage_prompt` at :797 passes no `paths` |
| Test presence | grep `test_all_bounds_chained_outputs_before_prompt_gate` / `test_chained_omission_marker_when_budget_below_source_count` / `test_chain_variables_bounds_total_chained_bytes` | **Absent** |
| Commit history | `git log` on `ai/run-review.py`; design-pipeline commits (`000bdd4`, `65e7544`, `ff9beb0`…) | Latest code touch is `c68c9e7` (old rollup); pipeline commits touched only `docs/auto/runs/…` artifacts |
| Design's factual claims | Line cites vs. live code | Accurate: unbounded paste :373–391, `_prompt_size_error` :507, `_fair_allowances` evidence.py:69, CHAIN_SOURCES shape, AC2 e2e assertions `stage-07=="8"`, `stage-09=="9"` at :223, monkeypatch idiom :184–190 |
| Style gate | `python quality.py ai/run-review.py` | OK (trivially — no changes exist) |

## Findings

| # | Sev | Defect pattern (testing.md §3) | Missing test → the exact test that would catch it |
|---|---|---|---|
| 1 | **Critical** | Implementation absent | Reproduce-first test (testing.md §2): the planned T2 fake agent emitting ≥30 KiB/stage under `--all` — today it fails with `exit != 0` and `stage-06/07/09` missing, because 276,712 B > 122,880 B. **This is the blocking gap: no code, no test, no commit.** |
| 2 | **Critical** | Planned tests unwritten | T1 (`test_chain_variables_bounds_total_chained_bytes`): `EVIDENCE_MAX_BYTES=4096`, 9×40 KiB → assert source-text sum ≤ 4096, all prefixes present, head-prefix property, in-budget byte-identity. T3 (`test_chained_omission_marker_when_budget_below_source_count`): `EVIDENCE_MAX_BYTES=3` → `count="6"`, 3 sources × 1 B. Neither exists anywhere in `tests/`. |
| 3 | **High** | Boundary case omitted from e2e | AC1's planned assertion set checks `prompt-stage-07` and `prompt-stage-09` only — **stage 06 is the marginal case** (design's own measurement: dies at ≥29.51 KiB/source; 4×30 KiB = 122,880 B + overhead > gate). Add: `assert len(prompt06.encode()) <= PROMPT_MAX_BYTES; assert "truncated" in prompt06`. Without it, the fix could regress only stage 06 and AC1 still passes. |
| 4 | **Medium** | Failure modes designed but never pinned | Design §2.5 lists 10 failure modes; only the main path (T1/T2/T3) has planned tests. Missing: `PROMPT_MAX_BYTES=1` → budget 0, all omitted, gate can't fire; `PROMPT_MAX_BYTES=0` → fall back to `EVIDENCE_MAX_BYTES`; `EVIDENCE_MAX_BYTES > PROMPT_MAX_BYTES//2` → cap at `prompt//2`; adversarial `prompt.max_bytes=1000` → gate still fires (final-arbiter property); multibyte boundary → no `\ufffd`, shown ≤ allowance. Each is one 5-line unit test with `monkeypatch.setattr(mod.batch_config, …)` — the idiom already used at :184–190. |
| 5 | **Medium** | Per-variable split invariant untested | The design's key arithmetic — stage 07's two vars get `budget//2 = 30720` each so 2×65536 can't re-trigger the gate — has no direct test (only indirect e2e). Add unit: 2 variables × 8 sources × 40 KiB → each var's source-text sum ≤ `budget//2`, total ≤ budget. |
| 6 | **Medium** | Property/fuzz layer missing (testing.md §4) | Truncation at byte boundaries is parser-class risk; `_fair_allowances` water-fill is allocation-class. No property test exists for either (grep: zero test files reference `_fair_allowances`). Add a small property test: random multibyte strings × random allowances → `len(shown.encode()) <= allowance`, `shown` is a byte-prefix of input, no `\ufffd`. |
| 7 | **Low** | Contract wiring gap (five layers, testing.md §1) | `_stage_prompt → chain_variables(paths=…)` is only covered via e2e. A direct unit test asserting markers carry real disk paths would pin the wiring and catch the "forgot to pass `paths`" regression class for ~5 lines. |
| 8 | **Info** | Citation drift | Design §1 cites tests at :57/69/80 (actual :57/66/75) and names `test_run_stage_rejects_oversized_prompt` (the `PROMPT_MAX_BYTES=1` block actually lives inside `test_run_stage_rejects_timeout` :184–190). Cosmetic, but it caused me to initially mis-locate the `_prompt_size_error` coverage. |

## Risk-coverage matrix

| Risky path | Covered now | Planned (T1–T3) | Gap |
|---|---|---|---|
| Sum-bound enforcement (R1) | ❌ | ✅ T1 + T2 | — |
| Omission marker, fail-closed (R2) | ❌ | ✅ T3 | — |
| Gate never re-triggered by chaining | ❌ | ✅ T2 | ⚠️ stage-06 leg missing (finding 3) |
| In-budget byte-identical compat (AC2) | ✅ existing e2e :223 | n/a | trivially green today — no change exists |
| Explicit value wins over budget | ✅ unit :66 | n/a | — |
| Missing sources skipped | ✅ unit :75 | n/a | — |
| Non-chained overflow → `_prompt_size_error` | ✅ :184–190 (`PROMPT_MAX_BYTES=1`) | n/a | — |
| `PROMPT_MAX_BYTES=0/1`, `EVIDENCE > prompt//2` degenerates | ❌ | ❌ | finding 4 |
| Multibyte truncation boundary | ❌ (prototype-only, uncommitted) | ❌ | finding 4/6 |
| `paths=None` / stale-path markers | ❌ | ❌ | finding 4/7 |
| Water-fill fairness arithmetic | ❌ (no test references `_fair_allowances`) | ❌ | finding 6 |
| Async reorder / duplicate submit / 409 / unmount | N/A | N/A | Single-threaded synchronous runner; no such surface exists — not a gap |
| Timeout uncertainty | ✅ :168 | n/a | pre-existing, unrelated to this change |

## Honesty audit (completion-evidence.md §2)

| Claim | Verdict | Evidence |
|---|---|---|
| Design §1 "every citation re-verified against the live repository" | ✅ **Verified** — I re-checked 8 of its line cites; all accurate, including the numeric refinement (122.1/242.3/272.0 KiB, thresholds 13.12/14.72/29.51 KiB) which my independent 30 KiB simulation confirms (276,712 B fails) | Design doc §1 vs. my greps/simulation |
| Design §4 "prototype run, live" (62,536 B stage 09, `count="6"`, no mojibake) | ⚠️ **Partial** — internally consistent and matches my simulation, but no prototype script/test is committed anywhere; the numbers cannot be re-run from repository artifacts | `grep -rn "chained-sources-omitted" --include="*.py"` → only `pbatch/evidence.py` hits |
| §2.6 "Add tests in tests/test_run_review.py (T1–T3)" | ❌ **not_executed — no reason stated** | Tests absent; no completion_report/`not_executed` section exists in the evidence |
| "Implementation: single commit, `ai/run-review.py` only" | ❌ **not_executed** | `git log` shows no such commit; worktree has no such diff |
| "Existing e2e passes unmodified (AC2)" | ⚠️ **Misleading as acceptance evidence** — it passes (21/21) but only because nothing changed; it proves nothing about the fix | pytest run |

**Executed vs. claimed commands:** the only commands whose outputs exist in the tree are the design's §1/§3 measurements (claimed; consistent with my re-runs) and the §4 prototype (claimed; **not reproducible from committed artifacts**). Every §2.6 gate (`pytest tests/test_run_review.py` → `make ci` → `quality.py --strict`) is **not_executed against any implementation** — there is no implementation. Per completion-evidence.md §2, the design should have stated "implementation and tests: not executed — this is a design-stage deliverable" explicitly; instead the absence is only discoverable by git archaeology.

**Bottom line:** the design is technically sound, its factual claims check out, and its test plan is the right shape (deterministic, monkeypatch-based, business-result assertions, no sleeps, no shared state, no skips — all testing.md §6 prohibitions respected). But it is a **design, not an implementation**: the defect it describes is live and reproduced, and the pipeline has stopped at the design stage with zero code and zero new tests. DoD (completion-evidence.md §3) is not met. Fix the two Critical findings first (implement + write T1/T2/T3), then close findings 3–6 before declaring done.
