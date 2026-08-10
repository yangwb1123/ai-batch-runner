All verification complete. Here is the product-thinker review.

---

## Product Thinker Review — Direction `product-specs-82a451b3` (billing question chain)

### 1. Verdict

```
VERDICT: PASS - the design adds the missing billing question chain as pure
question-generation data with fail-closed keyword alignment (triggers ⊆
registry, forbidden commercialization terms excluded), zero collateral surface
(C3 verified: only product.py data + doc row + 3 tests), L0 restraint pinned,
and no high-cost implementation anywhere. All design claims re-verified live.
```

### 2. Productization level + evidence

**Change's own level: L1 (reusable_module) — module data enhancement.** No platform/commercial/product intent in the change itself; depth driven by the change's scope, not by keyword greed.

Evidence:
- Assess of the change task text is confounded by keyword greed: `pbatch/product.py` in the path contains the Latin word `product` → `L3_product_feature, evidence: ["product"]`. This is itself a live demonstration of why level gating exists — and the design correctly does **not** let that artifact drive depth. The honest classification is module-level (additive data in one module, consumed through the existing public `scenario_questions`/`product_manifest` interface).
- The **governed behavior** is L3-facing: `assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` → `L3_product_feature, scenarios: []` — gap reproduced live, exactly as the requirements evidence claimed.
- Baselines re-verified: `tests/test_product.py` 15 passed; `python -m pbatch eval` → all 42 passed; `product.py` = 181 lines.

**L1 checks:** module boundary — one private data dict, no other code touched ✓; reusable interface — flows through existing rendering paths (`format_product`, `assess --json`) with zero consumer changes ✓; docs — `product-thinking.md` §3 row synced verbatim ✓; examples — acceptance commands (A1/A2) double as examples ✓.

### 3. Implicit-requirement questions (all 待确认 — requester decisions, not features)

| # | Question | Decision the requester must make | Recommendation |
|---|---|---|---|
| 1 | **Trigger boundary**: chain fires only on mechanism words (订阅/计费/定价/账单/套餐/配额/额度/quota/billing/subscription/pricing), **never** on intent-only inputs ("商业化"/"收费" without a mechanism word) | Confirm that a commercial *intent* without explicit billing *mechanism* stays question-free | Keep as designed — verified: "做一个开源上传产品，商业化收费" → only `['上传/文件']`, matching the eval guard |
| 2 | **plan/tier divergence**: registry `use_case_types.billing` contains `plan`/`tier`; the chain deliberately excludes them | Confirm the interim divergence until direction #3 unification resolves it | Acceptable — exclusion verified necessary (`planning`/`tier-1` produce no false hits); R3 test 3's subset guard keeps it one-directional and fail closed |
| 3 | **Multi-scenario interplay**: "企业订单系统，支持账单查询" fires both `ERP/订单` and `计费/订阅` | Confirm two concurrent question chains are desired for such inputs | Yes — both are question chains (not features); complementary, and FM-7's exactly-one assertion only pins A1 |
| 4 | **Non-L3 firing**: an L2 input ("多租户平台" + 配额) also gets billing questions (gate is level ≠ L0, not L3-only) | Confirm billing questions are acceptable for L2 platform inputs | Yes — quota/额度 is a low-cost reservation topic; asking is not designing |
| 5 | **发票与支付渠道** question edges into payment territory | Confirm it remains a question only, never an implied implementation | It is question-only by construction (§6 of product-thinking.md: 禁止未确认就实现) — no action |

### 4. Low-cost reservations / high-cost implementations

- **Low-cost reservations**: N/A to the change itself (library data change — nothing omitted within scope). Content-side: the emitted chain covers the low-cost reservation topics (配额/额度策略, 计量单位) as questions. Nothing low-cost is missing.
- **High-cost implementations**: **absent everywhere** — the change adds only question-generation data. No Billing/Subscription/计费 system, no pricing logic, no payment code. The existing double-gated `billing` rule (rules.yaml:57-64, `min_tier: production` + `page_types: [billing]`) is untouched. The design *strengthens* the restraint mechanism: the red line previously surfaced as a one-way `restraint_notes` warning with zero confirmation questions — the very gap this direction fixes. Justified ✓.

### 5. Restraint notes (what was deliberately NOT done, and why)

1. **No 收费/付费/商业化/支付/资金 triggers** — including them would make "开源上传产品，商业化收费" fire the billing chain and break the restraint discipline pinned by eval `l3-prescription-no-billing-without-signal`. Verified by simulation; also the reason `计费` (mechanism) was chosen over `收费` (intent).
2. **No plan/tier triggers** despite registry membership — generic Latin terms false-positive inside `planning`/`tier-1`. Verified: no Latin false hits.
3. **No `_SCENARIOS`↔registry unification** (direction #3) — deliberately out of scope; only a subset-guard test (R3 test 3) added, which fails closed if the registry degrades.
4. **No collateral changes** (C3) — rules.yaml, `_PRODUCT_SIGNALS`, level gating, `restraint_notes`, `_derive_specs_by_level`, `format_product`, assessor, eval cases all untouched; single 3-file commit with zero-residue rollback.
5. **No commercial-readiness.md edit** — its §2 trigger table ("商业化关键词 + 定价/收费/套餐") states a *higher bar* for the design red line; the chain is a lower-bar question generator. Residual Info-level divergence in trigger phrasing between the two docs is documented (FM-3) and accepted; a future unification could add one clarifying sentence, non-blocking.
6. **Honest gap disclosed, not hidden** — FM-2: the eval case guards only the rules/prescription layer, not the scenario layer; the mechanical tripwire (R3 test 3's forbidden-term assertion) is named explicitly. No unimplemented implicit feature is presented as done; the deliverable is a Proposed design awaiting implementation.

**Scope discipline**: directions #1 (prescription filtering) and #3 (registry unification) correctly excluded; the design does not creep into either.

---

**Review complete.** The change is the minimal, restraint-correct version of the fix: it converts the tool's most expensive red line (Billing) from a one-way warning into confirmed-questions, without ever crossing into Billing design itself. PASS.
