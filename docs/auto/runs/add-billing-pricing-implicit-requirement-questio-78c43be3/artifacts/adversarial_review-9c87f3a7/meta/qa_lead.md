All evidence gathered. Here is the QA lead review.

---

# QA Review — Billing/Subscription Question Chain (direction #2)

**Revision reviewed**: commit `e964fee` + campaign docs. **Implementation status: NOT YET APPLIED** — `_SCENARIOS` has 6 entries, no billing entry; `tests/test_product.py` has 15 tests (no billing tests); `product-thinking.md` §3 has 6 rows. This review therefore covers (a) baseline behavior, (b) adequacy of the proposed R3 test plan, and (c) two latent defects the design's own verification missed.

## 1. Test inventory — commands actually run (all current, live tree)

| Command | Result |
|---|---|
| `python -m pytest tests/test_product.py tests/test_assessor.py -q` | **24 passed** (15+9) |
| `python -m pbatch eval` | **all 42 passed** (incl. `l3-prescription-no-billing-without-signal`) |
| `assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` | L3, evidence `[saas]`, **`scenarios: []`** — gap reproduced exactly as claimed |
| `assess "做一个内部小工具脚本" --json` | L0, `scenarios: []`, specs `[]` — A2 shape confirmed |
| `rules "SaaS platform with tier-1 support, production" --json` | tier=production, `page_types: ['billing']`, **billing rule fires** ← new finding |
| `rules "multi-tier architecture, production go" / "tier 1 support…" / "production plan…" / "enterprise platform, production, payment integration" --json` | blast-radius probe (see F1) |
| `rules "做一个 SaaS 平台，订阅收费，按配额计费" --json` | tier=production, billing rule fires — post-R1 rules layer and scenario layer will agree (consistent) |
| Programmatic subset check | all 11 R1 trigger terms ∈ `use_case_types.billing` ✅; registry terms **not** in R1: `plan`, `tier`; R1 ∩ `risk.high` = 6 terms (订阅/计费/定价/billing/subscription/pricing) |
| `_keyword_hit` probes | `tier-1` **matches** word-boundary regex (dash = non-word); `planning` does not match `plan` ✅ |
| R1 simulation against live matcher | A1 → exactly `['计费/订阅']`; `商业化收费`/`商业化` → `['上传/文件']` only; `subscription billing and quota` → hit; Latin negatives clean |
| Level-override probes | `product_manifest('订阅计费配额', level='L0_local_feature')['scenarios'] == []`; `('小工具', level='L3…') == []` — §4 override claims hold |
| `python quality.py --strict pbatch/product.py` | OK (181 lines) |
| CI wiring | Makefile `ci` = check+self-test+test+quality+org — **no eval**; `.github/workflows/ci.yml` runs `make test` + `make eval` + `make coverage` |

## 2. Requirement-to-test matrix

| Requirement | Test (as designed) | Status | Evidence |
|---|---|---|---|
| A1 — billing chain fires, ≥3 questions (定价/计量/配额) | R3 test 1 (unit) + A1 integration command | Spec'd, **not yet written** | Gap reproduced live; simulation: exactly one entry |
| A2 — L0 restraint: `scenarios == []` | R3 test 2 + existing `test_l0_gets_no_scenarios_and_no_specs` | Spec'd | Verified live (A2 command) |
| C1 — trigger ⊆ registry; forbidden terms excluded | R3 test 3 (subset + forbidden) | Spec'd, **incomplete — see F2** | Subset ✅; forbidden list misses `支付`/`资金` |
| C2 — exactly five questions, noun-phrase style | R3 test 1 (`≥3` + content) | Spec'd, **weak — see F4** | `len == 5` is the exact-C2 pin |
| C3 — no collateral change | `test_assessor.py` (9) + eval (42) + quality + `make ci` | All green today | Measured |
| C4 — matcher semantics unchanged | none new (R1 reuses `_keyword_hit`) | ✅ but see F1 | Regex probes |
| FM-1 fail-closed registry degradation | R3 test 3 (empty registry → red) | Sound | `load_registry` degrades to `{}` → subset fails |
| Wire format (`assess --json` shape) | none new — **gap, see F3** | `test_assess_cli_product_json` covers generic path only | — |

## 3. Findings (severity-sorted)

### F1 — High (pre-existing; direction #3, but the design's verification is factually wrong)
**`plan`/`tier` in `use_case_types.billing` (product-specs/rules.yaml:32) cause a rules-layer false positive.** Verified: `"SaaS platform with tier-1 support, production"` → `page_types: ['billing']` → **billing rule fires** (commercial-readiness.md red line loaded) for a non-billing requirement. `_keyword_hit`'s Latin word boundary matches `tier-1` and `tier 1` (dash/space are non-word). Design §1 row 15 / §5.1 C4 / FM-6 claim "`tier-1` → no false hit" is **wrong as reasoning** — it holds only because `tier` is absent from R1 *triggers*; the regex itself matches, and the registry path is live. Not introduced by R1, but the design's own verification log records an incorrect conclusion.
**Untested failure**: registry→rules path with deployment-tier vocabulary.
**Recommendation**: record F1 as an explicit acceptance criterion for direction #3 (remove/gate `plan`/`tier` in the billing use_case_type + add a regression test with the `tier-1` input). For R1: add a comment on R3 test 3 noting that `plan`/`tier` must **never** be promoted into R1 triggers by the direction-#3 unification (would recreate the false positive at the scenario layer).

### F2 — Medium (in the designed tests themselves)
**R3 test 3's forbidden-term assertion omits `支付` and `资金`**, which C1 (§5.1) explicitly lists. `支付` is the single most dangerous term: a payment-feature requirement must not trigger subscription-billing questions (payment ≠ subscription mechanism — the restraint red line). As specified, a future edit adding `支付` to the triggers would pass R3 test 3 while violating C1 and the semantic anchor eval case (which guards only the rules layer — FM-2's documented gap).
**Exact test fix**: assert none of `[收费, 付费, 支付, 资金, 商业化, payment, commercial, monetize]` is a trigger term of the `计费/订阅` entry (all 5 C1 terms + English equivalents already in `risk.high`).

### F3 — Medium (integration gap)
**No automated pin on the A1 wire path.** R3 test 1 exercises `scenario_questions()` only; the `assess --json` aggregation/serialization path is covered generically by `test_assess_cli_product_json` (开源上传 input), never with a billing input. If assessor-side rendering ever drops/skips the new entry, all three R3 tests stay green and A1 fails.
**Exact test to add**: extend the existing `test_assess_cli_product_json` (capsys + `assess_main`) with the A1 input asserting `payload["product"]["scenarios"][0]["scenario"] == "计费/订阅"` and ≥3 questions. Zero new test functions — respects the "exactly 3 new tests" constraint.

### F4 — Low
**C2 says "exactly the five questions" but R3 test 1 asserts `len >= 3`.** A chain truncated to 3 questions covering 定价/计量/配额 passes. Strengthen to `len == 5` (satisfies A1's ≥3 floor, pins C2 verbatim, and indirectly pins 免费层/发票 presence by count).
**Acceptance assertion**: `assert len(billing["questions"]) == 5` and joined chain contains `定价` and `计量` and (`配额` or `额度`).

### F5 — Low (pre-existing, now relevant)
**The level-override path (`product_manifest(text, level=…)`) has zero unit tests.** The design's §4 API table explicitly promises it "unchanged"; a billing-keyword input with an L0 override must yield `scenarios == []` (verified true today). Fold into R3 test 2: `product_manifest("订阅计费配额", level="L0_local_feature")["scenarios"] == []` — pins the gating interaction R1 introduces.

### F6 — Info
**FM-3 (doc drift) is unguarded while FM-1 (code drift) is guarded — asymmetric.** The original incident this campaign fixes was a stale requirements doc; R2's `product-thinking.md` row has no mechanical pin. A 5-line test asserting the `| 计费/订阅 |` row exists in the §3 table is cheap. Design's 3-test cap is respected; record as prioritized optional follow-up (the highest-value cheap test after F2/F3).

### F7 — Info (doc mismatch, not a defect in the design)
`make ci` (local) does **not** run `make eval`; the 42 eval cases run only in GitHub Actions (`ci.yml` step "Rule-system eval suite"). AGENTS.md/`docs/CHECKS_REGISTRY.md` claim "make ci 含 25 eval 用例" — inaccurate for local `make ci`. The design's regression gates list eval as a manual gate, which is safe, but a local-only workflow would silently skip `l3-prescription-no-billing-without-signal`. Recommend: add `eval` to `make ci` or fix the docs.

### F8 — Info
FM-2's disclosed gap (eval guards rules layer, R3 test 3 guards scenario layer) is sound **only after F2 is fixed** — the tripwire must cover all C1-forbidden terms. No other finding: design §1 claims verified accurate (15/16 rows; the off-by-one `30-31` correction is right).

## 4. Prioritized scenario list (for R3 + manual acceptance)

1. **Happy** — A1 input → exactly one entry `计费/订阅`, 5 questions. (R3 t1)
2. **Happy, per-trigger** — each of 订阅/计费/定价/账单/套餐/配额/额度/quota/billing/subscription/pricing fires the chain; CJK substring vs Latin boundary semantics hold.
3. **Boundary** — minimal signal (`账单` alone) still fires; `额度` alone fires (registry parity with `配额`).
4. **Error/fail-closed** — registry YAML missing → subset test red (FM-1); L0 gate keeps `[]` even with billing keywords (F5 pin).
5. **Negative/restraint** — `做一个开源上传产品，商业化` / `…商业化收费` / `企业订单支付` stay billing-clean at the scenario layer; rules layer unchanged (eval anchor).
6. **Regression** — `tier-1`/`plan` rules-layer false positive (F1) tracked for direction #3; multi-scenario collision (FM-7): A1 exactly-one assertion as tripwire; insertion order cosmetic only (no order assertion needed — dict order guaranteed).
7. **Race/concurrency** — N/A: `scenario_questions`/`product_manifest` are pure, no shared mutable state, no I/O beyond a read-only YAML at import; parallel `assess` processes are independent. No race tests required.
8. **Recovery** — N/A: no new state, no persistence, no network. Rollback = revert one commit (zero residue) — sound.

## 5. CI gaps, flake risk, fixtures, exit criteria

- **CI gaps**: (a) `make ci` local lacks `eval` (F7); (b) `make coverage` covers only `--cov=checks --cov=cli` — `pbatch.product` has no coverage gate (acceptable for a data-driven entry, noted).
- **Flake risk**: none identified — all R3 tests are deterministic pure-function assertions; no time, network, or ordering dependence. `load_registry` reads from disk at import; no monkeypatching needed.
- **Fixtures needed**: none.
- **Exit criteria for the implementation** (in order): R3 tests written per §6 fail **red** today → apply R1+R2 → R3 **green**; `pytest tests/test_product.py tests/test_assessor.py -q` = 27 pass; `pi-batch eval` = 42; `quality.py --strict .`; `make ci` + GitHub Actions. **With QA amendments**: F2 (5-term forbidden list), F4 (`len == 5`), F5 (override pin), F3 (extend existing JSON test), and F1 recorded as an explicit acceptance criterion for direction #3.

**Bottom line**: the design's problem statement, API-additivity, and migration plan are verified sound; the R3 test plan is ~80% adequate and needs the F2/F3/F4 amendments before implementation to fully pin its own constraints. The one external defect (F1) is pre-existing, correctly out of scope, but must not be carried into the design's documentation as a verified "no false hit" — the reasoning is wrong even though the scenario-layer conclusion happens to hold.
