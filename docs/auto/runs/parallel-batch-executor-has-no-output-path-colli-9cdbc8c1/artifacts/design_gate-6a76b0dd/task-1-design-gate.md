## Gate verification — blocking findings vs. evidence

I independently re-verified every blocking finding and binding decision against the live tree, the design doc (`docs/auto/design/pbatch-ba9cd1a4-design.md`, 08:34), the adjudication (`pbatch-ba9cd1a4-adjudication.md`, 08:28), and the review harness (`/tmp/pbatch-review`, re-run: **41/41 pass**).

### Resolved with verified evidence ✅
- **qa F1** (T1b under-specified) — healed by C5 (stdout-only cwd participates in containment); §6 R1.2b carries the corrected fixture. Verified in `collides()`.
- **qa F2 / C6** (plan shape → TypeError/ValueError/silent-pass holes) — `isinstance` guards + digit-count guard + `try/except (TypeError, ValueError)` in `_node_index`; all shapes → `OutputCollisionError`. Reproduced in the design doc code and pinned by 8 harness tests.
- **qa F3** (wave budget-cap) — T2c in §6; harness `test_runner_budget_cap_wave_no_next_wave` passes.
- **architect #1/#4**, **C1–C4**, **B1** (998-line fold) — signed off with grounding and applied.

### Not resolved — contradicting evidence ⚠️
1. **async_reviewer #1 — the only HIGH finding (min_interval across barrier)**: adjudication line 77 records the binding owner decision **cumulative/global** (thread `successes` through `_collect_parallel`, zero net lines, pin `sleeps >= [1.0]`). The design doc — modified *6 minutes after* the adjudication — instead documents **per-wave** semantics ("`min_interval` throttles per wave (per-wave `_collect_parallel` counter — documented contract)", §2.2), §6 has no boundary-sleep test, and the harness has none either. No reversal is recorded anywhere (§0.2 lists C5/C6 only); backend_engineer's review notes "per-wave" without flagging the conflict. The implementable artifact contradicts the binding resolution of the only High finding, and its own §2.2 docstring ("throttles between successful completions") even contradicts its own prose.
2. **M1 (binding)**: `OutputCollisionError._report` still hardcodes "(exit 2)" — demonstrably wrong on the pipeline path (exit 1, verified no catch from `_execute_stage_tasks` to `_run_rounds`); §2.3's `log.error("%s", exc)` doesn't append it as mandated.
3. **M3 (binding)**: `--dry-run` collision warning explicitly deferred in §7 despite the mandate; migration/diff surface shows cli_batch +5, not the mandated +8–9.
4. **F5 (binding)**: pipeline `execute_stage` collision test absent from §6 and §5.
5. **M4 (binding)**: `--no-lock` absent from §6 R1.5 — and E1 is live *right now* (multiple campaign processes hold the run lock).
6. **B4 / B3 / L3 partial**: `docs/ARCHITECTURE.md:88` + `prompts/README.md` fixes missing from Phase 4; U4 case-scope and reuse-masking sentences absent from the §2.4 bullet; workers=1 / triangle / shuffled-plan pins absent from §6.

The core design and C5/C6/F2/F3 fixes are genuine and harness-verified, but three binding mandates plus the High finding's owner resolution are missing or contradicted in the artifact that Phase 2 will implement — with no recorded rejection.

VERDICT: FAIL - the only High finding (F4 min_interval) has a binding owner decision (cumulative) that the design doc contradicts with per-wave semantics and no recorded reversal, and binding mandates M1 (hardcoded "(exit 2)"), M3 (dry-run warning), F5 (pipeline test), M4 (--no-lock), plus B4/L3 items are absent from the design doc — the doc must be reconciled with the adjudication (or the reversals explicitly re-adjudicated with evidence) before implementation.
