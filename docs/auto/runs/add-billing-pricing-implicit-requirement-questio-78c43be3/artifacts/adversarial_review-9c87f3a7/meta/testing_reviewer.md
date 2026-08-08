All verification complete. Here is the review.

---

# QA Lead Review — Billing/Subscription Question Chain (direction #2) — testing-focused review

**Revision reviewed**: commit `e964fee` (design stage). **Implementation status: NOT APPLIED** — `_SCENARIOS` has 6 entries (no billing), `tests/test_product.py` has 15 tests (no billing tests), `product-thinking.md` §3 has 6 rows. This review therefore audits the **planned R3 test suite** against `backend-specs/testing.md` (five layers, reproduce-first, property/fuzz, isolation) and the honesty of all verification claims. Every claim below was re-run live in this session.

## 0. Baseline re-verification (all executed now, live tree)

| Command | Result |
|---|---|
| `python -m pytest tests/test_product.py tests/test_assessor.py -q` | **24 passed** (15+9) |
| `python -m pbatch eval` | **all 42 passed** (incl. `l3-prescription-no-billing-without-signal`) |
| `assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` | L3, evidence `[saas]`, **`scenarios: []`** — gap reproduced |
| `python quality.py --strict pbatch/product.py` | OK, 181 lines |
| R1 entry simulated exactly as designed (§3.2) against live matcher | A1 → exactly `['计费/订阅']` (hits 订阅/计费/配额); `商业化收费`/`商业化` → `['上传/文件']` only; `企业订单支付` → `['ERP/订单']` only; `支付功能`/`资金管理` → `[]` |
| `rules "platform with tier-1 support, production" --json` | **`page_types: ['billing']`** ← F1 reproduced (see H1) |
| `_keyword_hit` probes | `tier` matches `tier-1` and `tier 1` (word boundary, dash/space = non-word); `plan` does NOT match `planning` |
| Level-override probes | `product_manifest("订阅计费配额", level="L0_local_feature")["scenarios"] == []`; `("小工具", level="L3…") == []` — §4 override claims hold behaviorally, **but unpinned by any test** (M4) |
| Makefile | `ci: check self-test test quality org` — **no eval**; real 42-case suite runs only in GitHub Actions or manually; `tests/test_eval_suite.py` (22 pass) covers eval *machinery* with crafted tmp suites, not the real `evals/` |

Existing 15 tests: all pure-function business assertions, no sleeps, no fixtures, no shared state, no order dependence — isolation baseline is clean.

## 1. Verdict

```
VERDICT: FAIL - the planned R3 tests, as specified, would not catch the two most
likely regressions this direction exists to prevent: (F2) the forbidden-term
tripwire omits 支付/资金 — the exact terms C1 names — so a future edit adding
"支付" to the triggers passes R3 test 3 while violating the restraint red line
(the eval anchor guards only the rules layer, FM-2's own disclosed gap); and
(F3) no automated pin on the assess --json wire path, so assessor-side rendering
could drop the new entry with all three R3 tests green and A1 red. Additionally
(M1) migration §6 orders R1(code) before R3(tests), violating testing.md §2
reproduce-first — the tests would never demonstrate red. All three are cheap
pre-implementation amendments; after F2+F3+F4 + the §6 reorder, the plan is sound.
Baseline honesty: 24 tests / 42 eval / gap / 181 lines all re-verified true.
One incorrect conclusion recorded in the design's own verification log (H1).
```

## 2. Findings table

| # | Sev | Defect pattern (testing.md) | Missing test | Exact test case that catches it |
|---|---|---|---|---|
| H1 | High (pre-existing; **design doc incorrect**) | False-positive vocabulary in registry; verification recorded a wrong matcher-semantics conclusion | F1: `tier`/`plan` in `use_case_types.billing` (rules.yaml:30-32) → rules-layer false positive | `pbatch rules "platform with tier-1 support, production" --json` → **`page_types: ['billing']`** today (live). `_keyword_hit("tier-1", "tier") == True` (live). Design §5.1 C4/FM-6 claim "`tier-1` → no false hit" is **wrong as reasoning** — it holds only because R1 excludes `tier` from *triggers*; the regex matches. Fix: correct the design's verification log; record as acceptance criterion for direction #3 (remove/gate `plan`/`tier` + regression test with `tier-1` input); add a comment on R3 test 3 that direction #3 must **never** promote `plan`/`tier` into R1 triggers |
| M1 | Medium | testing.md §2 reproduce-first violated by migration order | R3 tests 1&3 fail **red** on today's tree (test 1: billing entry absent → assertion fails; test 3: entry missing → subset check fails) | Reorder §6: write R3 tests first → `pytest tests/test_product.py -q` = 17 passed / 2 failed (red confirmed) → apply R1 → green 19. As written (R1→R2→R3) the tests never fail once, so the fix's regression-proofness is unproven |
| M2 | Medium | Guard incomplete vs its own constraint (C1) | Forbidden-term assertion omits `支付` and `资金` (C1 lists all five; `risk.high` contains both; `支付` is the single most dangerous — payment ≠ subscription mechanism) | Strengthen R3 test 3: assert none of `[收费, 付费, 支付, 资金, 商业化, payment, commercial, monetize]` is a trigger of `计费/订阅`. As designed, adding `支付` to triggers passes R3 test 3 while `企业订单支付` would start firing subscription-billing questions and the eval anchor stays green (rules layer only — FM-2) |
| M3 | Medium | Integration layer gap (five-layer §1) | R3 test 1 exercises `scenario_questions()` only; `assess --json` aggregation/serialization is pinned generically (`test_assess_cli_product_json`, 开源上传 input), never with a billing input | Extend existing `test_assess_cli_product_json` (capsys + `assess_main`): A1 input → `payload["product"]["scenarios"][0]["scenario"] == "计费/订阅"` and `len(questions) >= 3`. Zero new test functions — respects the 3-test cap |
| M4 | Medium | Promised-unchanged path unpinned (completion-evidence: claims need tests) | §4 API table promises `product_manifest(text, level=…)` override "unchanged"; a billing-keyword input + L0 override must yield `[]` — true today, untested | Fold into R3 test 2: `product_manifest("订阅计费配额", level="L0_local_feature")["scenarios"] == []` — pins the L0×billing interaction R1 introduces |
| L1 | Low | Assertion weaker than the constraint it pins | C2 says "exactly the five questions"; R3 test 1 asserts `len >= 3` + content of only 定价/计量/配额 — a future edit deleting 免费层/发票 questions passes | Strengthen: `len(questions) == 5` (satisfies A1's ≥3 floor; pins C2 verbatim; count-pins the last two questions) |
| L2 | Low | No per-trigger / behavioral-negative coverage (property-style, testing.md §4) | A trigger term silently ceasing to match (e.g. Latin boundary regression) is unpinned; negative behavior is only transitively pinned via trigger content | Fold into R3 test 3 (same function, no new tests): parametrized `for t in triggers: assert any chain fires for t`; plus `scenario_questions("做一个开源上传产品，商业化收费")` has no `计费/订阅` entry |
| L3 | Low | FM-3 doc drift unguarded while FM-1 code drift is guarded (asymmetric; the original incident was a stale doc) | `product-thinking.md` §3 row has no mechanical pin | Optional follow-up (after M2/M3): 5-line test asserting the `| 计费/订阅 |` row exists in the §3 table |
| I1 | Info | Doc/CI mismatch | `make ci` (local) does not run `make eval`; AGENTS.md claims "make ci 含 25 eval 用例" — inaccurate for the real suite (local runs 22 machinery tests only) | Add `eval` to `make ci` or fix docs; the design's manual-gate list is safe but a local-only workflow silently skips `l3-prescription-no-billing-without-signal` |
| I2 | Info | FM-2 sound only after M2 | Tripwire must cover all C1-forbidden terms for the disclosed gap to hold | No action beyond M2 |

## 3. Risk-coverage matrix (testing.md §3 checklist, applicable subset)

| Risky path | Covered? | How / gap |
|---|---|---|
| Keyword drift (`_SCENARIOS` ↔ registry) | ✅ covered | R3 test 3 subset guard; fail-closed (`load_registry` degrades to `{}` → red) |
| Forbidden-term reintroduction (`支付`/`收费`…) | ⚠️ partial | **M2**: 收费/付费/商业化 only; 支付/资金 unpinned — blocking |
| Wire path (`assess --json` aggregation) | ❌ not covered | **M3**: generic JSON test only; billing-specific drop goes undetected |
| L0/L1 restraint regression | ✅ covered | R3 test 2 + existing `test_l0_gets_no_scenarios_and_no_specs`; **M4** adds override pin |
| Latin false positives (`tier-1`, `planning`) | ⚠️ partial | Excluded from R1 by construction; **H1**: rules layer is live-false-positive today; **L2** adds behavioral negative |
| Multi-scenario collision (FM-7) | ✅ covered | R3 test 1 exactly-one assertion turns red on collision |
| Matcher-semantics regression (`_keyword_hit`) | ⚠️ partial | **L2** per-trigger parametrize |
| Doc drift (FM-3) | ❌ not covered | **L3** optional pin |
| Registry degradation (FM-1) | ✅ covered | subset check fails closed |
| Ordering regression | ⚠️ cosmetic | membership-only assertions (dict order guaranteed); no action |
| Async reorder / duplicate submit / timeout / 409 / permissions / unmount / optimistic rollback / pagination / tz / partial success | ✅ N/A | `scenario_questions`/`product_manifest` are pure, no shared state, no I/O beyond read-only YAML at import; parallel processes independent — correctly no race tests needed |
| Property/fuzz (money allocation, parsers) | ✅ N/A | No money math, no parsers in this change; **L2** is the property-style guard that does apply |
| Test isolation / sleeps / order dependence | ✅ clean | Existing 15 + planned R3: pure assertions, no fixtures, no sleeps, deterministic |

## 4. Honesty audit (completion-evidence.md)

| Claim | Verdict |
|---|---|
| Design §1 rows 1-14, 16 (six entries, sole producer, registry @30-31, double-gated rule, doc tables, gap reproduction, eval 42, `_keyword_hit` semantics, 15+9 tests, subset ⊆ registry, 181 lines) | ✅ **Verified** — re-ran/read all live this session |
| Design §1 row 15 + §5.1 C4 + FM-6: "`planning`/`tier-1` → no false hits" | ⚠️ **Wrong as written (H1)** — `tier` DOES match `tier-1` (live: rules layer fires billing on `"platform with tier-1 support, production"`). The scenario-layer conclusion holds only because R1 excludes `tier`; the log records an incorrect matcher-semantics conclusion. Not fabrication (simulation was run), but the recorded reasoning is false and must be corrected |
| Design §9 verification log | ✅ honest — commands named with results, no "理论上应该通过" phrasing, no skipped checks; all re-runnable items confirmed |
| Design §7 acceptance commands | ✅ honest — stated as future gates, not claimed as executed |
| Status labeling | ✅ honest — deliverable is a **Proposed** design; no completion_report claims implementation; no tests added yet (implementation stage hasn't run) |
| product_thinker review (A1 exactly one; negatives clean) | ✅ Verified by exact-design simulation (corrected storage shape) |
| M4 override behavior | ✅ behaviorally true, ⚠️ unpinned by any test (finding M4) |
| I1 CI claim | ⚠️ AGENTS.md "make ci 含 25 eval 用例" inaccurate for local `make ci` (real suite is GitHub Actions-only); no fabricated command output |

**Bottom line**: baseline and design claims are honest and verified except H1's recorded reasoning. The R3 plan is ~70% adequate under the five-layer/reproduce-first lens; the F2/F3 amendments are **blocking** because they are precisely the tripwires for the two failure modes the direction exists to prevent. All amendments fit the existing 3-test budget (fold into current test functions). With those + the §6 test-first reorder, the implementation can proceed to red-green demonstration.
