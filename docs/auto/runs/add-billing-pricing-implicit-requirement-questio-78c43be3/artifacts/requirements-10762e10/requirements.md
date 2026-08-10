Requirements specification produced and persisted to `docs/auto/requirements/product-specs-82a451b3-billing-question-chain-requirements.md` (named uniquely to avoid clobbering the stale direction-#1 doc of the same analysis id).

## Key findings from live verification

**Gap confirmed.** `python -m pbatch assess "做一个 SaaS 平台，订阅收费，按配额计费" --json` → `level: L3_product_feature` (evidence: `saas`), but `product.scenarios == []`. All five citations verified with exact line numbers:

| Evidence | Verdict |
|---|---|
| `pbatch/product.py:58-96` | ✅ `_SCENARIOS` dict, exactly six entries (登录/认证…搜索), **no billing** |
| `pbatch/product.py:134-142` | ✅ `scenario_questions()` iterates only `_SCENARIOS` — sole producer of `product.scenarios` |
| `product-specs/rules.yaml:22-27` | ⚠️ miscited region; billing `use_case_type` is at **lines 30-32** (`订阅/计费/定价/账单/套餐/配额/quota/额度/billing/subscription/pricing/plan/tier`); `billing` rule at 57-64 is double-gated |
| `commercial-readiness.md:13-20` | ✅ §2 高成本实现 table, row 1 = Billing red line |
| `product-thinking.md:31-41` | ✅ §3 scenario table (six rows, no billing) |

**Root cause**: level gating (`product_manifest`, L0→no scenarios) is decoupled from scenario matching — an L3 commercial product hits no hardcoded scenario and gets zero confirmation questions, so the "先测量，后收费" restraint has no surfacing mechanism.

## Spec contents

- **R1** — one additive `计费/订阅` entry to `_SCENARIOS` (after 搜索): 5-question chain (定价模型/计量单位/配额策略/免费层 vs 付费层/发票与支付渠道), trigger keywords **subset of `use_case_types.billing`**, explicitly excluding 收费/付费/商业化 (verified `_keyword_hit` semantics: CJK substring, Latin word-boundary — `plan` can't hit `planning`). C1's forbidden-keyword exclusion is anchored to the live `l3-prescription-no-billing-without-signal` eval case (re-ran: green).
- **R2** — one-row sync of `product-thinking.md` §3 table (the drift that caused this gap).
- **R3** — three regression tests in `tests/test_product.py` (15 existing tests baseline).
- **Acceptance preserved and made testable** — A1/A2/A3 as exact commands, plus regression gates (`pytest tests/test_assessor.py`, `pi-batch eval`, `quality.py --strict`, `make ci`).
- **Scope discipline** — directions #1 (prescription filtering) and #3 (registry unification) explicitly out of scope; `rules.yaml`, `_PRODUCT_SIGNALS`, and level gating untouched.
