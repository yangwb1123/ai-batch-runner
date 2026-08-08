# Design — Billing/Subscription Question Chain for `_SCENARIOS`

Analysis: `product-specs-82a451b3` (direction #2: billing question chain)
Source: `docs/auto/requirements/product-specs-82a451b3-billing-question-chain-requirements.md`
Status: **Proposed** (evidence-verified design; implementation pending)
Scope: additive data change + doc sync + 3 regression tests. No CLI, schema, registry, or gate changes.

---

## 1. Evidence verification report (untrusted claims → verified)

Every claim in the requirements evidence was independently re-verified against the live tree
before this design was written. All verdicts are from direct reads/runs on
`/home/u1/ai-batch-runner` (commit state at time of writing).

| # | Evidence claim | Verification | Verdict |
|---|---|---|---|
| 1 | `_SCENARIOS` in `pbatch/product.py:58-96`, six entries (登录/认证…搜索), no billing | `grep -n` → dict opens at line 58; six entries; ends before `_LEVEL_TIERS` (line ~100) | ✅ exact |
| 2 | `scenario_questions()` (product.py:134-142) is the sole producer of `product.scenarios`, iterating only `_SCENARIOS` | Line 134 `def scenario_questions`; loop at 138 over `_SCENARIOS.items()`; `product_manifest` (144) is the only caller | ✅ exact |
| 3 | `product-specs/rules.yaml:22-27` miscited; `use_case_types.billing` at 30-32 | `grep -n` → `use_case_types:` at 23, auth…search at 24-29, `billing:` at **30-31** | ✅ (off-by-one: 30-31, not 32) |
| 4 | `billing` rule at rules.yaml:57-64 double-gated | Line 57 `billing:` rule; `min_tier: production` + `page_types: [billing]`; only `commercial-readiness.md` file | ✅ exact |
| 5 | `commercial-readiness.md:13-20` §2 table, row 1 = Billing red line | Table at 16-20; row 1 `Billing/Subscription/计费`, trigger `商业化关键词 + 定价/收费/套餐`, fallback `不设计；仅预留 billing 上下文字段` | ✅ exact |
| 6 | `product-thinking.md:31-41` §3 scenario table, six rows, no billing | Rows 33-38, six rows; §5 (line ~49) documents the Billing red line textually | ✅ exact |
| 7 | **Live gap**: `assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` → `L3_product_feature` with `scenarios == []` | Re-ran the command: `level: L3_product_feature`, `evidence: ["saas"]`, `scenarios: []`, `page_types: ["billing"]` | ✅ reproduced |
| 8 | Root cause: level gating decoupled from scenario matching | `product_manifest` gates on `resolved != "L0_local_feature"` (line 148), `_SCENARIOS` decides *which*; an L3 commercial product outside the six hardcoded scenarios yields zero questions | ✅ confirmed by code read |
| 9 | Restraint has no surfacing mechanism (one-way note only) | `restraint_notes` emits "高成本实现…禁止提前设计" for L2/L3 but no confirmation questions are produced; per product-thinking.md §3 all 推演 must be 待确认问题 | ✅ confirmed |
| 10 | Eval case `l3-prescription-no-billing-without-signal` exists, re-ran green | `evals/assess.yaml:33-39`: input `做一个开源上传产品，商业化`, asserts `lacks_rule: [billing, visual-core]`, `prescription_lacks_rule: [billing]`. `python -m pbatch eval` → **all 42 passed** | ✅ exact |
| 11 | `_keyword_hit` semantics: CJK substring, Latin word-boundary | `relevance.py:27-34`: CJK → `term in text`; Latin → `(?<!\w)…(?!\w)` | ✅ exact |
| 12 | 15 existing tests in `tests/test_product.py`; 9 in `tests/test_assessor.py` | `grep -c "def test_"` → 15 / 9; `pytest tests/test_product.py -q` → 15 passed | ✅ exact |
| 13 | Requirements doc persisted at uniquely-named path | `docs/auto/requirements/product-specs-82a451b3-billing-question-chain-requirements.md` (17,176 bytes) alongside the stale `…-requirements.md` (dir has both) | ✅ exact |
| 14 | R1 trigger keyword list ⊆ `use_case_types.billing` | Programmatic check: all 11 terms ∈ `load_registry(domain="product")['use_case_types']['billing']` | ✅ exact |
| 15 | A1 input yields exactly one scenario under R1; eval/negative inputs stay clean | Simulated R1 entry against live `_SCENARIOS` + `_keyword_hit`: A1 input → `['计费/订阅']` only; `做一个开源上传产品，商业化`/`…商业化收费` → `['上传/文件']` only; `planning`/`tier-1` (Latin) → no false hits; English `subscription billing and quota` → hits | ✅ verified by simulation |
| 16 | `product.py` is 181 lines; ~8-line addition stays under budget | `wc -l` → 181 | ✅ exact |

**Verification verdict: the requirements evidence is reliable.** One imprecision
(`billing` use_case_type at 30-31 not 30-32) and one nuance requiring design-level
attention — see §5 FM-2 (the eval case guards the *rules/prescription* layer, not the
*scenario* layer; the mechanical scenario guard is R3 test 3, not the eval case).

---

## 2. Problem statement (verified)

A commercial product requirement that names an explicit billing mechanism
(`订阅收费，按配额计费`) is classified L3 via `saas`, but produces **zero
confirmation questions** because:

1. `_SCENARIOS` hardcodes six domains (登录/认证、上传/文件、审批流、消息/聊天、
   ERP/订单、搜索) — no billing entry;
2. level gating (L0 → no scenarios) and scenario matching are decoupled, so an L3
   product whose domain isn't in the six gets `scenarios == []`;
3. the "先测量，后收费" restraint (commercial-readiness.md §2 red line) surfaces only
   as a one-way `restraint_notes` warning, not as questions-to-confirm — the two-way
   confirmation mechanism required by product-thinking.md §3/§6 is missing.

Consequence: the most expensive high-cost decision in the product spec (Billing) is
the one least likely to be confirmed with the requester.

---

## 3. Design

### 3.1 Approach

One additive `_SCENARIOS` entry (data, not logic) + one doc-table row + three
regression tests. No public API signature changes, no matching-code changes, no
registry changes, no eval-case changes.

### 3.2 R1 — `_SCENARIOS` entry (pbatch/product.py, after the `搜索` entry)

```python
    "计费/订阅": (
        ["订阅", "计费", "定价", "账单", "套餐", "配额", "额度", "quota",
         "billing", "subscription", "pricing"],
        ["定价模型（订阅/按量/混合）", "计量单位与用量上报",
         "配额/额度策略与超限处理", "免费层 vs 付费层", "发票与支付渠道"],
    ),
```

Dict insertion order = output order, so `计费/订阅` appears last in
`product.scenarios` and in `format_product`'s `推演[…]` lines. Existing six entries
and their relative order are untouched.

### 3.3 R2 — doc sync (product-specs/product-thinking.md §3, after the 搜索 row)

```
| 计费/订阅 | 定价模型（订阅/按量/混合）？计量单位与用量上报？配额/额度策略与超限处理？免费层 vs 付费层？发票与支付渠道？ |
```

Label and chain verbatim from R1. `commercial-readiness.md` needs no change (§2
already documents trigger signals and the no-signal fallback).

### 3.4 R3 — regression tests (tests/test_product.py, +3)

1. **Billing chain fires** (pins A1): `scenario_questions("做一个 SaaS 平台，订阅收费，按配额计费")`
   → exactly one entry; `scenario` contains `计费` or `订阅`; `len(questions) >= 3`;
   joined chain contains all of `定价`, `计量`, `配额` (or `额度`).
2. **L0 restraint intact** (pins A2): `product_manifest("做一个内部小工具脚本")` → `scenarios == []`.
3. **Keyword alignment** (pins C1): every trigger term of the `计费/订阅` entry ∈
   `load_registry(domain="product")["use_case_types"]["billing"]`; and
   `收费/付费/商业化` are **not** trigger terms. Data-consistency guard for this
   entry only (full `_SCENARIOS`↔registry unification stays direction #3, out of scope).

---

## 4. API changes

| Surface | Change | Type |
|---|---|---|
| `pbatch.product.scenario_questions(text)` | behavior: new possible output entry for billing-keyword inputs | behavioral (additive) — signature/return shape unchanged |
| `pbatch.product.product_manifest(text, level=None)` | behavior: `scenarios` may now contain `计费/订阅` for L1+ inputs; L0 unchanged (`[]` via existing gate) | behavioral (additive) — signature unchanged |
| `pbatch.product._SCENARIOS` | +1 entry (module-private data) | internal additive |
| `assess --json` output | `product.scenarios` list may contain the new `{"scenario": "计费/订阅", "questions": [...]}` object — same shape as the six existing entries | wire-format additive |
| `assess` human output | new `推演[计费/订阅]: …` line when triggered | additive |
| `product_manifest(text, level=L0_…)` override path | unchanged (level override bypasses keyword leveling; scenario gating still applies) | none |
| Downstream consumers (pipeline prompts, `product_thinker` role, `format_product`) | receive the new entry via existing rendering paths; no consumer code changes | none |

No new CLI flags, no config keys, no schema fields, no new modules.

---

## 5. Compatibility constraints & failure modes

### 5.1 Constraints (binding)

- **C1 — keyword alignment**: trigger terms ⊆ `use_case_types.billing`
  (rules.yaml:30-31). Forbidden: `收费/付费/支付/商业化/资金` (risk.high terms —
  rules.yaml:34-36). Including them would make e.g. "做一个开源上传产品，商业化收费"
  trigger the billing chain and violate the restraint discipline pinned by
  `l3-prescription-no-billing-without-signal`.
- **C2 — question chain**: exactly the five questions; ≥ 3 (A1 floor); covers
  定价/计量/配额/免费层 vs 付费层/发票与支付渠道; noun-phrase style consistent
  with the six existing chains.
- **C3 — no collateral change**: `_PRODUCT_SIGNALS`, `product_manifest` level
  gating, `restraint_notes`, `_derive_specs_by_level`, `format_product`,
  `pbatch/assessor.py`, `rules.yaml`, `evals/assess.yaml` — all unchanged.
- **C4 — matcher semantics**: reuse `_keyword_hit` as-is (CJK substring, Latin
  word boundary). Verified: `plan`/`tier` (excluded from triggers) would not hit
  `planning`/`tier-1` anyway; included Latin terms (`quota`, `billing`,
  `subscription`, `pricing`) hit at word boundaries only.

### 5.2 Failure modes and mitigations

| # | Failure mode | Trigger | Mitigation |
|---|---|---|---|
| FM-1 | **Keyword drift** — `_SCENARIOS` billing triggers diverge from `use_case_types.billing` (registry edits add/remove terms) | future registry change | R3 test 3 (subset guard) fails closed; note: `load_registry` degrades to empty `use_case_types` if YAML is missing → subset check fails → test red (fail closed, correct direction) |
| FM-2 | **Forbidden keyword reintroduced** into the trigger (e.g. `收费`) | future edit | ⚠️ **Not caught by the eval case**: `l3-prescription-no-billing-without-signal` asserts only `product_level`/`has_rule`/`lacks_rule`/`prescription_lacks_rule` — the *rules/prescription* layer, which R1 never touches. The mechanical scenario-layer guard is R3 test 3's `收费/付费/商业化 not in trigger terms` assertion. This is a deliberate, documented gap: the eval case is a semantic anchor, R3 test 3 is the tripwire. |
| FM-3 | **Doc drift** — product-thinking.md §3 table falls behind `_SCENARIOS` again | future scenario edits | R2 syncs the row now. No mechanical guard in scope (direction #3 territory); residual risk is documented and accepted. Optional hardening (a test asserting the row exists in the md) is a cheap future follow-up, deliberately not added to keep R3 at exactly three tests per the requirements spec. |
| FM-4 | **Ordering regression** — insertion order changed (new entry not last, or existing entries reordered) | future edit | No existing test asserts exact scenario order; R3 tests assert membership, not order. Dict order is preserved by Python; the design appends after 搜索. Risk is cosmetic (output order) only. |
| FM-5 | **L0/L1 restraint regression** | future edit of level gate | R3 test 2 pins `product_manifest("做一个内部小工具脚本") → scenarios == []`; existing `test_l0_gets_no_scenarios_and_no_specs` stays. |
| FM-6 | **False positive on Latin inputs** (`plan`, `tier` are the risky generic English terms) | — | Mitigated by construction: `plan`/`tier` deliberately excluded from triggers (simulation: `planning`/`tier-1` → no hit). Remaining Latin terms are billing-specific vocabulary. |
| FM-7 | **Multi-scenario interplay** — an input hitting billing + another scenario changes A1's "exactly one entry" | future keyword additions elsewhere | A1's exactly-one assertion holds today (verified by simulation: A1 input hits only `计费/订阅`). If a future scenario keyword collides, R3 test 1's `len(...) == 1` turns red — surface it, don't weaken the assertion without revisiting A1. |
| FM-8 | **Code organization gate** — file size/complexity | — | `product.py` = 181 lines today; the ~8-line entry keeps it far under the 1000-line budget; `quality.py --strict .` gate covers it. |
| FM-9 | **`plan` vs `planning` CJK/Latin mixing in pipeline prompts** | — | `_keyword_hit` lowercases input; CJK substring unaffected by case; Latin word-boundary verified. No action. |

---

## 6. Migration steps

This is a single-repo library data change — no data migration, no rollout,
no feature flags. Ordered implementation and verification sequence:

1. **R1** — append the `计费/订阅` entry to `_SCENARIOS` in `pbatch/product.py`
   (after the `搜索` entry, ~line 95). Verify no other code touched.
2. **R2** — add the row to `product-specs/product-thinking.md` §3 table (after the
   `搜索` row, line ~38).
3. **R3** — add the three tests to `tests/test_product.py`.
4. **Acceptance runs** (in order, fail fast):
   - `python -m pbatch assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` → A1 shape
   - `python -m pbatch assess "做一个内部小工具脚本" --json` → A2 shape
   - `python -m pytest tests/test_product.py tests/test_assessor.py -q` → 27+ pass (15+9+3)
   - `python -m pbatch eval` → all 42 pass (restraint case included)
   - `python quality.py --strict .` → clean
   - `make ci` → full regression suite per repo AGENTS.md
5. **Commit** — single commit touching exactly three files
   (`pbatch/product.py`, `product-specs/product-thinking.md`, `tests/test_product.py`).

Rollback: revert the commit. Because the change is additive data, rollback is
complete and has zero residue (no schema, no generated files).

---

## 7. Testable acceptance mapping

| # | Acceptance | Testable command / assertion | Pinned by |
|---|---|---|---|
| A1 | `assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` → `product.scenarios` contains a billing/计费 scenario with ≥ 3 questions (定价/计量/配额 etc.) | `python -m pbatch assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` → `product.scenarios` is a list of exactly one entry with `scenario` containing `计费` or `订阅`, `len(questions) >= 3`, joined text contains all of `定价`, `计量`, `配额` (or `额度`) | R3 test 1 (unit-level) + A1 command (integration) |
| A2 | `assess "做一个内部小工具脚本" --json` → `scenarios` still `[]` (L0 restraint) | `python -m pbatch assess "做一个内部小工具脚本" --json` → `product.scenarios == []`, `product.level == "L0_local_feature"` | R3 test 2 + A2 command |
| A3 | `python -m pytest tests/test_product.py -q` passes | ≥ 18 tests (15 existing + 3 new) all green | R3 tests + A3 command |
| C1 | keyword alignment | R3 test 3 (subset + forbidden-term assertions) | R3 test 3 |
| C2 | question chain content | R3 test 1 (`定价`/`计量`/`配额` coverage, ≥ 3) | R3 test 1 |
| C3 | no collateral change | code-review diff: exactly 3 files; `pytest tests/test_assessor.py` (9) + `pi-batch eval` (42) + `quality.py --strict` + `make ci` | regression gates |

**Regression gates (must stay green):**
- `python -m pytest tests/test_product.py tests/test_assessor.py -q` (24 existing + 3 new)
- `python -m pbatch eval` (42 cases; includes `l3-prescription-no-billing-without-signal`)
- `python quality.py --strict .`
- `make ci`

---

## 8. Scope boundary

**In:** one `_SCENARIOS` entry (R1); one doc row (R2); three tests (R3).

**Out (explicitly):** direction #1 (prescription filtering of commercial/open-source
specs by the empty-signals demo cap — `pbatch/assessor.py`), direction #3
(`_SCENARIOS`↔`use_case_types` unification + dead rule-level `risk:` metadata
removal), any `rules.yaml`/`_PRODUCT_SIGNALS`/level-gating/eval-case change,
packaging/CLI/pipeline/gate changes.

---

## 9. Verification log (this document)

| Step | Result |
|---|---|
| Read `pbatch/product.py` (full) | `_SCENARIOS` 6 entries; `scenario_questions` sole producer; 181 lines |
| Read `product-specs/rules.yaml` (full) | billing use_case_type @30-31; billing rule @57-64 double-gated |
| Read `commercial-readiness.md` §2 / `product-thinking.md` §3 | red-line table + 6-row scenario table confirmed |
| Ran `assess … --json` (gap input) | L3 + `scenarios: []` — gap reproduced |
| Ran `python -m pbatch eval` | all 42 passed (restraint case green) |
| Ran `python -m pytest tests/test_product.py -q` | 15 passed |
| Simulated R1 entry against live matcher | A1 → exactly `['计费/订阅']`; eval inputs → no billing hit; Latin negatives clean |
| Programmatic subset check | all 11 R1 trigger terms ∈ `use_case_types.billing` |
