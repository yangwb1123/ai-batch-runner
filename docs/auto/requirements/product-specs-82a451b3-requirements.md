# Requirements Specification — Declarative rules.yaml registry for product-specs + rule_matcher domain mapping

| Field | Value |
|---|---|
| Module | `product-specs` |
| Direction id | `82a451b3` (direction #1 of `docs/auto/analyses/product-specs-82a451b3.json`) |
| Source analysis | `docs/auto/analyses/product-specs-82a451b3.json` |
| Status | Proposal (evidence-verified, design simulation-validated) |
| Scope | This direction only — directions #2 (scenario keyword tightening) and #3 (execution-prompt spec paths) are **out of scope** (§6) |

---

## 1. Summary

`backend-specs/rules.yaml` and `ui-specs/rules.yaml` are declarative rule
registries consumed by `pbatch/rule_matcher.py`'s domain mapping
(`DOMAIN_REGISTRIES`, `rule_matcher.py:41-45`), giving the backend and
frontend domains the full rule-system contract: scale × page_type/use_case ×
risk three-axis filtering, REQUIRED-rule fail-closed, `pi-batch rules --check`
schema validation, and LLM two-sided reconcile. `product-specs/` has none of
this: its spec selection is hardcoded in `pbatch/product.py:85-92`
(`_SPECS_BY_LEVEL`), loaded whole-per-level with no tier/risk filtering, no
schema validation, no two-sided check, and `rules --check` validates only the
two existing domains (`rule_cli.py:122`). Extending the product spec assets
(e.g. a new payment-scenario chain) requires editing Python instead of a
declarative registry, contradicting the registry decision chain documented in
`docs/SPEC_SYSTEM.md` §2-§3.

Verified baseline (live, this repository):

- `python -m pbatch rules --check` → `[frontend_ui] registry: OK`,
  `[backend] registry: OK` — **product domain absent**.
- `python -m pbatch rules "做一个开源上传产品，商业化" --json` →
  `domain: generic, tier: standard, rules: [], skipped: []` — the task gets
  **zero** product specs today (acceptance A3 currently fails).
- `python -m pytest tests/test_product.py` → 13 passed; `pi-batch eval` →
  41 passed (regression baselines that must stay green).
- **Merge-inheritance hazard (critical implementation detail):**
  `load_registry` resets to an empty base only for `domain in ("backend",
  "generic")` (`rule_matcher.py:100`). A product domain added to
  `DOMAIN_REGISTRIES` without extending that tuple would **inherit the
  frontend defaults** (`visual-core`, `component-spec`, `business-profile`
  rules + ui-spec files) — verified by simulation. The reset tuple must
  include `"product"`.
- Packaging: `pyproject.toml` `[tool.setuptools.package-data]` deliberately
  ships only `pbatch/*.yaml`; the spec registries (ui/backend) are resolved
  repo-root- and CWD-relative by `_default_registry_paths`. `product-specs/
  rules.yaml` follows the same convention — **no packaging change required**
  (documented deferral in `tests/test_packaging.py`).

---

## 2. Evidence verification

Every citation from the direction was re-verified against the current
repository (line numbers confirmed live).

| Cited evidence | Verification result |
|---|---|
| `pbatch/product.py:85` | ✅ **Confirmed.** `_SPECS_BY_LEVEL = {` at **line 85**, dict spans 85–92 (L0 → `[]`, L1 → product-thinking + completion-evidence, L2 → + commercial-readiness, L3 → + open-source-readiness). Consumed at `product.py:140` (`"specs": _SPECS_BY_LEVEL.get(resolved, [])` in `product_manifest`). |
| `pbatch/rule_matcher.py:42` | ✅ **Confirmed.** `DOMAIN_REGISTRIES` at **lines 41–45**: `"backend": ["backend-specs/rules.yaml"]` at 42, `"frontend_ui": ["ui-specs/rules.yaml"]` at 43, `"generic": []` at 44. |
| `pbatch/rule_matcher.py:43` | ✅ **Confirmed** (see above). |
| `backend-specs/rules.yaml` | ✅ **Confirmed.** Declares `scale / signals / signal_weights / use_case_types / risk / rules`; 20+ rules with `min_tier`, `required`, `required_when_risk`, `page_types` (referencing `use_case_types` entries), `profiles`. |
| `ui-specs/rules.yaml` | ✅ **Confirmed.** Same contract; `level: invariant` on `visual-core`, `suppress_on` on two rules. |
| `product-specs/product-thinking.md` | ✅ **Confirmed.** §4 "无开源意图时不加载（克制）", §5 "**高成本实现**（有明确商业信号才做，禁止提前设计）：完整 Billing/Subscription/计费系统、插件市场、多级 License", §6 "无商业化信号不设计 Billing/Subscription". |
| `docs/SPEC_SYSTEM.md` | ✅ **Confirmed.** §2 registry table lists exactly two registries (ui/backend); §3 decision chain (`classify → rules → assess → …`); §7 restraint principles. |

Additional live verifications used in the design:

| Claim | Verification |
|---|---|
| `rules --check` domain tuple | `pbatch/rule_cli.py:122` — `domains = ("frontend_ui", "backend")`; docstrings at 119–121 and `--check` help at 153 say "双域" |
| `load_registry` reset tuple | `pbatch/rule_matcher.py:100` — `if domain in ("backend", "generic"):`; simulation confirmed a naive product domain leaks frontend default rules |
| `domain_for` fallback | `rule_matcher.py:78-86` — returns `DEFAULT_DOMAIN` ("generic") for non-backend/frontend tasks; "做一个开源上传产品，商业化" classifies `unknown → generic` today |
| selfcheck wiring | `pbatch/selfcheck.py:37` runs `python -m pbatch rules --check` as the "registry" step of `pi-batch check`; comment at `selfcheck.py:5` says "both domain rule registries" |
| Import-cycle feasibility | `pbatch/system_type_methodology.py` (imported by rule_matcher) has no imports; `rule_matcher` does not import `product` — a lazy import of `productization_level` inside `domain_for` (or module-level `rule_matcher` import in `product.py`, but not both at module level) is cycle-free |
| eval machinery | `pbatch/eval.py:104-122` `_assert_value` dispatch; set-semantics tuple at `eval.py:138` (`"has_rule", "includes", "lacks_rule", "excludes"`) — extendable for prescription-level keys |
| No existing tests pin `_SPECS_BY_LEVEL` contents | `tests/test_product.py` asserts membership only (open-source-readiness in L3 specs, empty specs at L0); no order assertions |

**Design simulation (scratch, monkeypatched):** the proposed registry
(§3 R1) + domain routing + derivation were simulated against the live
matcher: 0 schema violations from `check_registry`; `rules "做一个开源上传
产品，商业化"` → `domain: product, tier: production, risk: high`, rules
include `commercial-readiness` (files → `product-specs/commercial-readiness.md`),
`billing` skipped with `page type not matched`; derived per-level spec sets
match the current `_SPECS_BY_LEVEL` exactly for all four levels; a task with
explicit billing mechanics (`"做一个 SaaS 计费产品，订阅制收费"`) does load
the `billing` rule.

---

## 3. Requirements

### R1 — New declarative registry `product-specs/rules.yaml`

Create `product-specs/rules.yaml` following the same contract as
`backend-specs/rules.yaml` / `ui-specs/rules.yaml` (sections `scale`,
`signals`, `signal_weights`, `use_case_types`, `risk`, `rules`; every rule
with non-empty `description`, valid `min_tier`, and existing `files`). The
following is the **normative content** (validated: 0 `rules --check`
violations, all referenced files exist):

```yaml
# Rule registry: which product-spec files apply to which task.
# Consumed by pbatch/rule_matcher.py (domain "product", selected for
# product-signal tasks by pbatch/product.py productization_level L1+).
# L0 tasks never reach this registry (restraint); the level→tier mapping
# for product_manifest specs derivation is L1=demo, L2=standard, L3=production.
scale:
  demo: [小工具, 脚本, 内部, 临时, 工具, demo, 原型, utility, script, internal, prototype]
  production: [产品, 商业化, 开源, 上线, 对外, 发布, saas, product, commercial,
               open source, release]

# Empty on purpose: the assess effective tier (处方档 = min(关键词档, 规模档))
# must stay demo (S) for product tasks, so production-tier high-cost rules
# (billing) never enter the prescription (restraint, product-thinking.md §5).
signals: {}
signal_weights: {}

use_case_types:
  auth: [登录, 认证, auth, login, sso, oauth]
  upload: [上传, 文件, upload, file, 附件, oss, s3]
  approval: [审批, approval, approve, 工作流, workflow]
  messaging: [聊天, 消息, chat, message, 即时通讯]
  erp: [erp, 订单, 采购, 库存, 财务, order]
  search: [搜索, 检索, search, lookup]
  billing: [订阅, 计费, 定价, 账单, 套餐, 配额, quota, 额度, billing,
            subscription, pricing, plan, tier]

risk:
  high: [支付, 审批, 资金, 收费, 商业化, 订阅, 计费, 定价, 付费,
         payment, approve, commercial, subscription, billing, monetize, pricing]

rules:
  product-thinking:
    description: 产品化分级与需求推演（L0-L3 决策表、隐含需求推演链、过度产品化红线）
    files: [product-specs/product-thinking.md]
    min_tier: demo
    required: true
  completion-evidence:
    description: 完成证据报告（诚实完成报告，禁止伪造通过表述）
    files: [product-specs/completion-evidence.md]
    min_tier: demo
  commercial-readiness:
    description: 商业项目思维（低成本预留 vs 高成本实现、License 分级、Billing 克制红线）
    files: [product-specs/commercial-readiness.md]
    min_tier: standard
    risk: [high]
    required_when_risk: true
  open-source-readiness:
    description: 开源项目思维（目录/文档套件/工程配套；无开源意图不加载）
    files: [product-specs/open-source-readiness.md]
    min_tier: production
  billing:
    description: >-
      高成本实现（Billing/Subscription/计费系统/插件市场）：仅在有明确商业机制
      信号（订阅/计费/定价/账单/套餐/配额）时加载；无商业信号禁止提前设计
      （先测量，后收费）
    files: [product-specs/commercial-readiness.md]
    min_tier: production
    page_types: [billing]
```

Design notes (binding):
- `billing` is **double-gated**: `min_tier: production` + `page_types:
  [billing]` (a use_case_type triggered only by explicit commercial-mechanics
  keywords). "商业化" alone must **not** load it (verified: skipped with
  `page type not matched`).
- `commercial-readiness` uses `required_when_risk` so it loads only with a
  commercialization/risk signal — consistent with product-thinking.md §5
  restraint; the level-based `product_manifest` specs (R3) still include it
  per level (unchanged documented contract).
- `signals`/`signal_weights` stay **empty**: `scale_signal` keeps returning S
  for product tasks, so `assess` effective tier stays demo and no production
  rule can enter the prescription (second, independent guard for A4).
- The rule for scenario chains (`use_case_types` upload/auth/approval/…)
  duplicates `_SCENARIOS` keywords by design: the registry is the declarative
  source for the matcher's axes; tightening `_SCENARIOS` itself is direction
  #2 (out of scope, §6).

### R2 — rule_matcher domain mapping: add `product` domain

In `pbatch/rule_matcher.py`:

1. `DOMAIN_REGISTRIES` (line 41-45): add
   `"product": ["product-specs/rules.yaml"]`.
2. `load_registry` reset tuple (line 100): `if domain in ("backend",
   "generic", "product"):` — **mandatory**, otherwise the product domain
   inherits frontend default rules (verified hazard, §1).
3. `domain_for` (lines 78-86): after the `backend` / `frontend_ui` checks
   (precedence preserved), route product-signal tasks:
   ```python
   from .product import productization_level  # lazy: avoids import cycle
   if productization_level(text)[0] != "L0_local_feature":
       return "product"
   return DEFAULT_DOMAIN
   ```
   - L1/L2/L3 productization levels → `product` domain; L0 (小工具/脚本/
     内部/临时/…) → stays `generic` (restraint: L0 gets no specs).
   - Lazy import is **required**: `pbatch/product.py` will import
     `load_registry` at module level (R3), so `rule_matcher` must not import
     `product` at module level (cycle).
   - Routing is text-driven and uses the existing `productization_level`
     vocabulary (no classifier/keyword duplication).

### R3 — `pbatch/product.py`: derive `_SPECS_BY_LEVEL` from the registry

Remove the hardcoded `_SPECS_BY_LEVEL` literal (lines 85-92) and derive it at
module level from `load_registry(domain="product")`:

- Level→tier mapping: `L0_local_feature → none (specs = [])`,
  `L1_reusable_module → demo`, `L2_platform_capability → standard`,
  `L3_product_feature → production`.
- For each level, collect `files` of every rule with
  `TIER_ORDER[rule.min_tier] <= TIER_ORDER[level_tier]`, deduplicated in
  registry declaration order (verified: this reproduces the current sets
  exactly for all four levels; the L3 file order differs cosmetically from
  today — no test pins order).
- Fail closed: if the registry is missing/empty (rules section empty), log a
  warning and yield `[]` per level — never fall back to hardcoded lists.
- `product_manifest` (line 140) keeps using `_SPECS_BY_LEVEL.get(resolved,
  [])` unchanged; module docstring updated to reference the registry.
- Add `from .rule_matcher import TIER_ORDER, load_registry` at module level
  (no cycle given R2's lazy import).

### R4 — `rules --check` covers the product domain

In `pbatch/rule_cli.py`:

1. `_check_registries_cli` (line 122): `domains = ("frontend_ui", "backend",
   "product")` — output must include `[product] registry: OK`.
2. Update wording: docstring (119-121) "validate both domain registries" →
   three domains; `--check` help (line 153) "双域" → "双域+product"（三域）.
3. `pbatch/selfcheck.py:5` comment "both domain rule registries" → three
   (the `pi-batch check` "registry" step reuses `rules --check` automatically).

### R5 — Eval regression: prescription-level assertion keys + new case

1. `pbatch/eval.py` `_assert_value` (lines 104-122): add
   `prescription_has_rule` / `prescription_lacks_rule` returning
   `{item["id"] for item in prescription(input_text)["prescription"]}`
   (import `prescription` alongside the existing `workflow_level` import).
2. `eval.py` `run_suite` set-semantics tuple (line 138): extend to
   `("has_rule", "includes", "lacks_rule", "excludes",
   "prescription_has_rule", "prescription_lacks_rule")` so both new keys get
   the same all-present / none-present list semantics.
3. Document the two keys in the `eval.py` module docstring assertion-key list.
4. `evals/assess.yaml`: add one case (must be `--filter`-targetable, unique id):

```yaml
  - id: l3-prescription-no-billing-without-signal
    input: "做一个开源上传产品，商业化"
    assert:
      product_level: L3_product_feature
      has_rule: [product-thinking, commercial-readiness]
      lacks_rule: [billing]
      prescription_lacks_rule: [billing]
```

   - `has_rule` pins acceptance A3 to CI (commercial-readiness must appear in
     the rules manifest for this input).
   - `lacks_rule` pins the matcher-level page-type gate; `prescription_lacks_rule`
     pins the prescription-level guarantee (处方档) the acceptance demands —
     the case fails loudly if anyone later escalates product signals or
     removes the billing gate.

### R6 — Documentation sync (small, in-scope)

- `docs/SPEC_SYSTEM.md` §2 registry table: add a row
  `| product-specs/rules.yaml | 产品 | scale × use_case_type × risk（L0 克制不加载；L1=demo/L2=standard/L3=production）|`.
- `AGENTS.md`: add the product registry to the `pbatch/rule_matcher.py` /
  `product-specs/` component rows; update "双域" wording in the `rules
  --check` quick-reference line to 三域.

---

## 4. Acceptance criteria (preserved from the direction, made testable)

| # | Acceptance (original) | Testable command / assertion |
|---|---|---|
| A1 | `product-specs/rules.yaml` 存在且 `pi-batch rules --check` 通过（含 product 域 schema 校验） | `python -m pbatch rules --check` → exit 0, log contains `[product] registry: OK` |
| A2 | `pbatch/product.py` 的 `_SPECS_BY_LEVEL` 移除或改为从注册表派生后，`tests/test_product.py` 全部通过 | `python -m pytest tests/test_product.py` → 13 passed |
| A3 | `pi-batch rules "做一个开源上传产品，商业化" --json` 返回的处方包含 `product-specs/commercial-readiness.md` | `python -m pbatch rules "做一个开源上传产品，商业化" --json` → `domain: "product"` and some `rules[].files` contains `product-specs/commercial-readiness.md`; also pinned by the new eval case (R5) |
| A4 | 新增 eval 用例（`evals/assess.yaml`）断言 L3 处方档不加载 product 高成本实现（Billing）规则，`pi-batch eval` 通过 | `python -m pbatch eval` → all pass (41 existing + 1 new = 42 cases), including `l3-prescription-no-billing-without-signal`; `python -m pbatch eval --filter l3-prescription-no-billing-without-signal --json` → `{"total": 1, "failed": 0, "failures": []}` |

Regression gates (must stay green after implementation):
`python -m pbatch eval` (42), `python -m pytest tests/` (387), `python
quality.py --strict .`, `pi-batch check` (selfcheck incl. the extended
`rules --check`), `make ci`.

---

## 5. Files touched

| File | Change |
|---|---|
| `product-specs/rules.yaml` | **new** — R1 normative content |
| `pbatch/rule_matcher.py` | R2 — `DOMAIN_REGISTRIES` (41-45), reset tuple (100), `domain_for` (78-86) + lazy import |
| `pbatch/product.py` | R3 — remove `_SPECS_BY_LEVEL` literal (85-92), derive from registry |
| `pbatch/rule_cli.py` | R4 — domains tuple (122), docstrings (119-121, 153) |
| `pbatch/selfcheck.py` | R4 — comment (line 5) |
| `pbatch/eval.py` | R5 — assertion keys (104-122), set-semantics tuple (138), docstring |
| `evals/assess.yaml` | R5 — new case |
| `docs/SPEC_SYSTEM.md`, `AGENTS.md` | R6 — registry table row, 双域→三域 wording |

Optional new unit tests (recommended, within scope):
- `tests/test_product.py`: derived L3 spec set == the four product-spec files,
  L0 == `[]` (parity pin).
- `tests/test_rule_matcher.py` (or `test_product.py`): `domain_for("做一个
  开源上传产品，商业化") == "product"` and `domain_for("写个内部小工具脚本，
  解析日志") == "generic"` (L0 restraint pin).

---

## 6. Out of scope (explicitly not part of this direction)

- Direction #2 of the same analysis: tightening `_SCENARIOS` keywords
  (pbatch/product.py:53-68) and adding negative eval cases for scenario
  false-positives.
- Direction #3 of the same analysis: injecting level-matched product-spec
  paths into `assessor.format_execution_prompt` (the "评估→执行" dead-path
  fix). Note: this registry is a prerequisite for that direction but is not
  required by it.
- Changing `classify_text` / `task_keywords.yaml` task types (domain routing
  happens in `domain_for` only; pipeline routing is untouched).
- Changing `load_registry` fail-open behavior for missing registries
  (consistent with the existing backend degrade-to-empty contract).
- Packaging `product-specs/rules.yaml` into the wheel (documented deferral
  in `tests/test_packaging.py`; loaded repo-root/CWD-relative like the
  existing ui/backend registries).
- Any change to `_PRODUCT_SIGNALS` / `_SCENARIOS` vocabularies in
  `pbatch/product.py` (they remain the level/scenario detectors; the registry
  only governs the matcher's axes and the specs derivation).
