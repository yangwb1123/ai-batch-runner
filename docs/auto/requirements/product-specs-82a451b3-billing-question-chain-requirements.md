# Requirements Specification — Add billing/pricing implicit-requirement question chain (highest-cost red line has zero scenario coverage)

| Field | Value |
|---|---|
| Module | `product-specs` |
| Direction id | `82a451b3` (direction #2 of `docs/auto/analyses/product-specs-82a451b3.json`) |
| Source analysis | `docs/auto/analyses/product-specs-82a451b3.json` (direction #2) |
| Status | Proposal (evidence re-verified live against current tree) |
| Scope | This direction only — directions #1 (prescription filtering) and #3 (registry unification) are **out of scope** (§5) |

---

## 1. Summary

Billing is the highest-cost, red-lined product decision — `product-specs/commercial-readiness.md` §2 requires an explicit commercial-mechanism signal before any Billing/Subscription design ("先测量，后收费"; §4 克制红线: "无商业信号不建 Billing"), and `pbatch/product.py`'s restraint machinery emits `restraint_notes` warning "高成本实现（需明确商业信号才做）：Billing/Subscription/插件市场禁止提前设计". Yet the **question-chain mechanism** that surfaces implicit requirements to the requester has **zero billing coverage**: `_SCENARIOS` (pbatch/product.py:58–96) contains six scenarios (登录/认证, 上传/文件, 审批流, 消息/聊天, ERP/订单, 搜索) — no billing/计费/定价/订阅/支付 entry — while the declarative registry `product-specs/rules.yaml` already declares a `billing` use_case_type (`rules.yaml:30-32`) and a double-gated `billing` rule (`rules.yaml:57-64`).

**Verified live (this repository):**

```
$ python -m pbatch assess "做一个 SaaS 平台，订阅收费，按配额计费" --json
→ product.level = "L3_product_feature" (evidence: ["saas"])
→ product.scenarios = []            # ← the gap: zero questions for a commercial product
→ product.restraint_notes contains the Billing red-line warning
```

The only scenario signal of a commercial product therefore produces **zero questions** to confirm with the requester (定价模型/计量单位/配额策略/免费层 vs 付费层/发票与支付渠道), so the restraint discipline has no mechanism to surface. The fix is a single additive change: one `_SCENARIOS` entry whose trigger keywords are a subset of the registry's `billing` use_case_type (alignment, not duplication drift), whose question chain names the five topics above, plus the spec-asset table mirror (`product-specs/product-thinking.md` §3) and regression tests. No registry, level-signal, or prescription logic changes.

---

## 2. Evidence verification

Every citation from the direction was re-verified against the current repository (line numbers confirmed live).

| Cited evidence | Verification result |
|---|---|
| `pbatch/product.py:58-96` — `_SCENARIOS` dict | ✅ **Confirmed, exact.** `_SCENARIOS = {` at **line 58**; dict spans lines 58–96 with exactly six entries: 登录/认证 (59–62), 上传/文件 (63–66), 审批流 (67–70), 消息/聊天 (71–75), ERP/订单 (76–80), 搜索 (81–84). **No billing/计费/定价/订阅/支付 entry.** Each entry is `label: (terms, chain)` where `chain` is the implicit-requirement question list. |
| `pbatch/product.py:134-142` — scenario dispatch | ✅ **Confirmed, exact.** `def scenario_questions(text)` at **line 134**; loop 138–142 iterates `_SCENARIOS.items()` and appends `{"scenario": label, "questions": list(chain)}` on any `_keyword_hit`. No billing terms → no billing questions, regardless of level. |
| `product-specs/rules.yaml:22-27` | ✅ **Confirmed with refinement.** The cited region is the `use_case_types:` block header + first entries (lines 22–27 = comment + `auth`/`upload`/`approval`/`messaging` headers). The **billing use_case_type sits at lines 30–32**: `billing: [订阅, 计费, 定价, 账单, 套餐, 配额, quota, 额度, billing, subscription, pricing, plan, tier]`. Also verified: `risk.high` (lines 34–36) includes 支付/收费/商业化/订阅/计费/付费/payment/commercial/subscription/billing/monetize/pricing; the `billing` rule (lines 57–64) is double-gated (`min_tier: production` + `page_types: [billing]`). |
| `product-specs/commercial-readiness.md:13-20` | ✅ **Confirmed, exact.** Line 13 is `## 2. 高成本实现（需明确商业信号，禁止提前设计）`; lines 14–20 are the trigger-signal table whose first row is `Billing/Subscription/计费 | 商业化关键词 + 定价/收费/套餐 | 不设计；仅预留 billing 上下文字段`. Line 20/§4 克制红线: `无商业信号不建 Billing`. |
| `product-specs/product-thinking.md:31-41` | ✅ **Confirmed, exact.** Line 31 is `## 3. 隐含需求推演（产出"问题清单"，不是"功能清单"）`; lines 33–41 are the 场景→推演问题 table (登录/认证, 上传/文件, 审批流, 消息/聊天, ERP/订单, 搜索 — six rows, **no billing row**). §5 (商业项目思维) restates the red line: "无商业化信号时禁止设计 Billing（'先测量，后收费'）". |

Supporting behavioral verification (live, this repository):

| Claim | Verification |
|---|---|
| Commercial-mechanics input → L3 but `scenarios == []` | `python -m pbatch assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` → `level: L3_product_feature`, evidence `["saas"]`, `scenarios: []`, specs include commercial-readiness; restraint notes warn about Billing red line (gap: no questions to confirm it) |
| L0 restraint currently works (must be preserved) | `python -m pbatch assess "做一个内部小工具脚本" --json` → `level: L0_local_feature`, `scenarios: []`, `specs: []` |
| Keyword matcher semantics | `pbatch/relevance.py:27-34` `_keyword_hit`: CJK terms = substring match; Latin terms = word-boundary regex `(?<!\w)term(?!\w)` — so `plan`/`tier`/`pricing` cannot false-positive inside English words (e.g. `planning`) |
| Output rendering | `format_product` (product.py:151-164) renders each scenario as `推演[<label>]: q1 / q2 / ...`; `scenario` label and `questions` appear verbatim in `assess --json` → `product.scenarios` |
| Test baseline | `tests/test_product.py` exists with **15 tests**; `test_l0_gets_no_scenarios_and_no_specs` (line 35) and `test_l3_gets_scenarios_specs_and_restraint` (line 43) pin scenario/restraint behavior |
| Registry billing keywords are the alignment source | `rules.yaml:30-32` — `use_case_types.billing` is the single declarative home of billing-mechanism keywords (this direction reuses a **subset**; full unification of the dual registries is direction #3, out of scope §5) |

---

## 3. Root-cause analysis

`scenario_questions` (product.py:134–142) is the **only** producer of `product.scenarios`, and it iterates only `_SCENARIOS` (product.py:58–96). Three independent facts compound:

1. **Coverage gap**: `_SCENARIOS` has no billing entry, although `use_case_types.billing` (rules.yaml:30–32) declares 13 billing keywords and the matcher path `page_types: [billing]` (rules.yaml:64) proves billing is a recognized use-case axis. The scenario axis and the registry axis have drifted (this drift is direction #3's subject — out of scope here).
2. **Signal mismatch**: the assess input "做一个 SaaS 平台，订阅收费，按配额计费" hits the L3 level via the `saas` product signal (product.py:18–28 `_PRODUCT_SIGNALS`) and would hit `use_case_types.billing` via 订阅/计费/配额 — but level determination and scenario determination are **decoupled**: level gates *whether* scenarios run at all (`product_manifest` line 148: `if resolved != "L0_local_feature"`), while `_SCENARIOS` decides *which* scenarios match. A commercial product therefore produces `scenarios == []` whenever its domain isn't one of the six hardcoded scenarios.
3. **Restraint without surfacing**: `restraint_notes` correctly emits "高成本实现…禁止提前设计" for L3 (product.py:152–154), but the note is a one-way warning; the two-way mechanism that turns red-lined decisions into **questions to confirm** (定价模型/计量单位/配额策略/免费层 vs 付费层/发票与支付渠道 — the exact dimensions commercial-readiness.md §2 gates on) is missing. Per product-thinking.md §3, 推演出的维度必须是待确认问题 — with zero questions, nothing is confirmed or declined; the discipline silently degrades.

**Fix shape**: one additive `_SCENARIOS` entry. Trigger keywords must be a **subset of `use_case_types.billing`** (rules.yaml:30–32) so the scenario axis stays aligned with the declarative axis (and deliberately **excludes** 收费/付费/商业化 — those are `risk.high` signals, not billing-mechanism signals; the registry's design note at rules.yaml:18–20 pins "商业化 alone must not load billing", and the same discipline applies to the scenario trigger). The question chain must cover the five topics named in the direction: 定价模型, 计量单位, 配额策略, 免费层 vs 付费层, 发票与支付渠道. Questions are confirmations, never features (product-thinking.md §3 / §6; commercial-readiness.md §4).

---

## 4. Requirements

### R1 — Add `计费/订阅` scenario entry to `_SCENARIOS` (pbatch/product.py:58–96)

Append one entry to `_SCENARIOS`, after the `搜索` entry (dict ends line 96; insertion order = output order in `scenario_questions` / `format_product`; existing six entries and their order must be untouched):

```python
    "计费/订阅": (
        ["订阅", "计费", "定价", "账单", "套餐", "配额", "额度", "quota",
         "billing", "subscription", "pricing"],
        ["定价模型（订阅/按量/混合）", "计量单位与用量上报",
         "配额/额度策略与超限处理", "免费层 vs 付费层", "发票与支付渠道"],
    ),
```

Binding constraints (all evidence-backed):

- **C1 — keyword alignment**: every trigger term must be an element of `use_case_types.billing` (rules.yaml:30–32). The set above is such a subset. **Forbidden**: 收费, 付费, 支付, 商业化, 资金 (these are `risk.high` terms — rules.yaml:34–36 — not billing-mechanism signals; including them would make e.g. "做一个开源上传产品，商业化收费" trigger the billing chain and break the `l3-prescription-no-billing-without-signal` restraint eval case in `evals/assess.yaml`).
- **C2 — question chain**: exactly the five questions above (each is a question to confirm, in the existing noun-phrase style; the chain must cover 定价 / 计量 / 配额 / 免费层 vs 付费层 / 发票与支付渠道 so the direction's acceptance A1 is satisfiable). The chain must contain ≥ 3 questions (A1 floor).
- **C3 — no behavior change elsewhere**: `_PRODUCT_SIGNALS` (product.py:17–28), `product_manifest` level gating (product.py:144–149), `restraint_notes` (product.py:150–155), `_derive_specs_by_level` (product.py:100–120) and `format_product` (product.py:151–164) are **unchanged**. L0 stays `scenarios == []` via the existing level gate (A2).
- **C4 — matcher semantics**: `_keyword_hit` (relevance.py:27–34) applies automatically (CJK substring, Latin word boundary) — no matching-code change.

### R2 — Spec-asset sync: `product-specs/product-thinking.md` §3 scenario table

Add one row to the 场景→推演问题 table (lines 33–41), matching the R1 label and chain verbatim:

```
| 计费/订阅 | 定价模型（订阅/按量/混合）？计量单位与用量上报？配额/额度策略与超限处理？免费层 vs 付费层？发票与支付渠道？ |
```

Rationale: this table is the human-readable mirror of `_SCENARIOS`; leaving it stale recreates exactly the drift that caused this gap (documented by analysis direction #3). This is a one-row doc sync of the same content — not a new feature. `commercial-readiness.md` needs **no** change (§2 table already documents trigger signals and the no-signal fallback).

### R3 — Regression tests in `tests/test_product.py` (15 existing tests must stay green)

Add focused tests pinning the acceptance criteria:

1. **Billing chain fires** (pins A1): `scenario_questions("做一个 SaaS 平台，订阅收费，按配额计费")` returns exactly one entry; its `scenario` contains `计费` or `订阅`; `len(questions) >= 3`; the joined chain contains all of `定价`, `计量`, `配额` (or `额度`).
2. **L0 restraint intact** (pins A2): `product_manifest("做一个内部小工具脚本")` → `scenarios == []` (strengthens the existing `test_l0_gets_no_scenarios_and_no_specs` guard with the exact acceptance input).
3. **Keyword alignment** (pins C1): every trigger term of the `计费/订阅` entry is a member of `use_case_types.billing` from the product registry (`load_registry(domain="product")`), and 收费/付费/商业化 are **not** trigger terms. This is a data-consistency guard for this entry only — full registry unification remains direction #3 (out of scope).

---

## 5. Scope boundary

**In scope:**
- One `_SCENARIOS` entry in `pbatch/product.py` (R1) + the five-question chain.
- One-row sync of `product-specs/product-thinking.md` §3 table (R2).
- Focused regression tests in `tests/test_product.py` (R3).

**Out of scope (explicitly not part of this direction):**
- Direction #1 of the same analysis (`prescription` filtering: commercial/open-source specs filtered out by the empty-signals demo cap — `pbatch/assessor.py:161`, `format_execution_prompt` injection) — separate direction, separate change.
- Direction #3 of the same analysis (unifying `_SCENARIOS` with `use_case_types`, removing dead rule-level `risk:` metadata in `rules.yaml:50-52` / `ui-specs/rules.yaml:143` / `backend-specs/rules.yaml:62`) — the alignment guard in R3 test 3 is the *minimum* drift protection, not the unification.
- Any change to `product-specs/rules.yaml` (billing use_case_type, billing rule, signals) — the registry is already correct for this gap.
- Any change to `_PRODUCT_SIGNALS` / level gating / `restraint_notes` / specs derivation / `assessor` prescription logic / eval cases (`evals/assess.yaml`).
- Packaging, CLI, pipeline, or gate changes.

---

## 6. Acceptance criteria (preserved from the direction, made testable)

| # | Acceptance (original) | Testable command / assertion |
|---|---|---|
| A1 | `assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` → `product.scenarios` contains a billing/计费 scenario with ≥ 3 questions (定价/计量/配额 etc.) | `python -m pbatch assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` → `product.scenarios` is a list of exactly one entry with `scenario` containing `计费` or `订阅`, `len(questions) >= 3`, and the joined question text contains all of `定价`, `计量`, `配额` (or `额度`). Also pinned by R3 test 1. |
| A2 | `assess "做一个内部小工具脚本" --json` → `scenarios` still `[]` (L0 restraint) | `python -m pbatch assess "做一个内部小工具脚本" --json` → `product.scenarios == []`, `product.level == "L0_local_feature"`. Also pinned by R3 test 2. |
| A3 | `python -m pytest tests/test_product.py -q` passes | `python -m pytest tests/test_product.py -q` → 15 existing + new tests all pass (≥ 18). |

**Regression gates (must stay green after implementation):**
- `python -m pytest tests/test_product.py tests/test_assessor.py -q` (product + assess paths; test_assessor.py has 9 tests)
- `python -m pbatch eval` (eval suite includes `l3-prescription-no-billing-without-signal` restraint case — verifies C1's forbidden-keyword exclusion)
- `python quality.py --strict .` (repo organization gate; product.py is 181 lines today — the ~8-line `_SCENARIOS` addition keeps it far under the 1000-line budget)
- `make ci` per repo AGENTS.md for changes to this tool

---

## 7. Files touched

| File | Change |
|---|---|
| `pbatch/product.py` | R1 — append `计费/订阅` entry to `_SCENARIOS` (after 搜索 entry, before line 96) |
| `product-specs/product-thinking.md` | R2 — one row in §3 scenario table (lines 33–41) |
| `tests/test_product.py` | R3 — 3 new tests (billing chain fires; L0 restraint with exact acceptance input; keyword-subset alignment guard) |

---

## 8. Verification script (exact commands for the implementer)

```bash
# A1 — before: scenarios == [] (the gap); after: one 计费/订阅 scenario, >=3 questions
python -m pbatch assess "做一个 SaaS 平台，订阅收费，按配额计费" --json \
  | python -c 'import json,sys; d=json.load(sys.stdin)["product"]; s=d["scenarios"]; \
    assert len(s)==1 and ("计费" in s[0]["scenario"] or "订阅" in s[0]["scenario"]), s; \
    q=" ".join(s[0]["questions"]); assert len(s[0]["questions"])>=3 and all(k in q for k in ("定价","计量","配额")), q'

# A2 — L0 restraint unchanged
python -m pbatch assess "做一个内部小工具脚本" --json \
  | python -c 'import json,sys; d=json.load(sys.stdin)["product"]; assert d["scenarios"]==[] and d["level"]=="L0_local_feature"'

# A3 — regression suite
python -m pytest tests/test_product.py -q
python -m pytest tests/test_product.py tests/test_assessor.py -q
python -m pbatch eval
python quality.py --strict .
```
