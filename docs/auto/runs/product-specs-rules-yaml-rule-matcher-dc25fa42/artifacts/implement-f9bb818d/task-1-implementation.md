# Implementation Report — Declarative rules.yaml registry for product-specs + rule_matcher domain mapping

| Field | Value |
|---|---|
| Module | `product-specs` (direction `82a451b3`; campaign `product-specs-rules-yaml-rule-matcher-dc25fa42`) |
| Design | `docs/auto/design/product-specs-82a451b3-design.md` (R1–R6, D1–D5, A1–A4, F1–F9) |
| Status | Implemented, all lock-free gates green; full `make ci` `test` target blocked by the ongoing campaign's repo lock (21 environmental failures, exit-5 signature) |

---

## 1. Analysis (mandatory thinking order)

1. **Business boundary**: The capability is "which product-spec files apply to which task" — a rule-system concern. It is owned by `pbatch/rule_matcher.py` (matcher + domain routing) with `product-specs/rules.yaml` as the declarative asset of the product domain; `pbatch/product.py` consumes the registry for the level→specs derivation. `rules --check` (schema gate) lives in `pbatch/rule_cli.py`.
2. **Data ownership**: The registry YAML is the single source of truth (append-only, declarative, schema-checked). `rule_matcher` owns loading/routing; `product` owns the derivation; neither writes back to the registry. Public contract: `load_registry(domain="product")` returns the merged rule map; `_SPECS_BY_LEVEL` retains its name so `product_manifest` and all consumers (`assessor.py:41`, `profile.py:29`, `eval.py:49`) are source-compatible with zero edits.
3. **Strong consistency**: No DB state involved. The only atomicity requirement is *derivation determinism*: `_SPECS_BY_LEVEL` is computed once at module import from the registry (declaration-order dedup), fail-closed on missing/empty registry (warn + `[]` per level, never a hardcoded fallback).
4. **Change points**: What varies is (a) the rule set (new spec files, tier/risk/page-type gates) — now data, not code; (b) the domain routing — one tuple in `DOMAIN_REGISTRIES` + the reset tuple in `load_registry` + `domain_for`. Nothing else varies; no abstraction introduced beyond the existing registry contract.
5. **Dependency direction**: `product → rule_matcher` at module level; `rule_matcher → product` **only** via the lazy import inside `domain_for` (cycle break, commented at both sites). Verified by smoke import of `pbatch.rule_matcher, pbatch.product, pbatch.assessor`.
6. **Pattern decision — explicit: no pattern, composition suffices.** The derivation is a 15-line data-driven loop over the registry; the lazy import is a Python module-semantics requirement, not a design pattern. Rule of three does not trigger (this is the *third* registry instance following the established ui/backend convention — the convention itself is the abstraction, and it is data, not class hierarchy). No interface per class, no factory, no Base class added.
7. **Persistence gate**: Not applicable — direction is doc/data-only (declarative YAML + deterministic matcher). No core entities, tables, ORM, money, or concurrency. The registry *is* the persistence artifact and is validated by `rules --check` (schema, referenced-file existence, self-reference rejection, and now the R4′ non-empty guard).
8. **Production readiness**: contracts unchanged (all signatures preserved); error model: `rules --check` exits 1 on empty/missing registry (fail closed); idempotency: derivation is pure and repeatable; observability: `log.warning` on missing registry, per-domain `[product] registry: OK` log line; verification honestly reported in §4 (including the 21 environmental failures and the two design-doc deviations below).

## 2. Implementation

### Files touched

| File | Change | Symbols |
|---|---|---|
| `product-specs/rules.yaml` | **new** (R1 normative content) | 5 rules: product-thinking, completion-evidence, commercial-readiness, open-source-readiness, billing; empty `signals`/`signal_weights`; use_case_types incl. billing |
| `pbatch/rule_matcher.py` | R2 wiring | `DOMAIN_REGISTRIES` +`"product"`; reset tuple `("backend","generic","product")`; `domain_for` lazy-imports `productization_level`, L1+ → `"product"`, L0 → generic |
| `pbatch/product.py` | R3 derivation | imports `log` (`.config`), `TIER_ORDER, load_registry` (`.rule_matcher`, cycle-guard comment); `_LEVEL_TIERS`; `_derive_specs_by_level()`; `_SPECS_BY_LEVEL = _derive_specs_by_level()` (name retained) |
| `pbatch/rule_cli.py` | R4 + R4′ | `_check_registries_cli`: `domains = ("frontend_ui","backend","product")`; non-empty guard (empty/missing rules → exit 1); docstring + `--check` help 三域 |
| `pbatch/selfcheck.py` | R4 | docstring comment "three domain rule registries" |
| `pbatch/eval.py` | R5 (D4-corrected) | module import `prescription`; `_assert_value` new branch (`prescription_has_rule`/`prescription_lacks_rule` → prescription id set); `_list_assertion_failure` both tuple extensions; `run_suite` tuple +2 keys; module docstring +2 keys |
| `evals/assess.yaml` | R5 + D2 | new case `l3-prescription-no-billing-without-signal` (asserts `product_level`, `has_rule`, `lacks_rule: [billing, visual-core]`, `prescription_lacks_rule: [billing]`) |
| `tests/test_product.py` | D3/F6 pins | `test_derived_specs_parity_l3_and_l0`, `test_domain_for_routes_product_and_generic` |
| `tests/test_rule_matcher.py` | F1/F2 pins | `test_product_registry_has_no_frontend_defaults`, `test_check_registries_fails_closed_on_empty_registry` |
| `docs/SPEC_SYSTEM.md` | R6 | §2 registry table row for product + classifier-first precedence note |
| `AGENTS.md` | R6 | rule_matcher + product component rows; `rules --check` 双域→三域 |
| `README.md` | R6+ | "both domains" → "three domains" (identical stale wording; minimal sync) |

### Evidence-backed deviations from the design doc (flagged, not silent)

1. **`from .rule_matcher import LEVELS` omitted (design §2.2).** `rule_matcher.LEVELS` is the *rule-level taxonomy* (`invariant/contract/policy/heuristic/suggestion`), not productization levels. `product.py` already defines its own `LEVELS` (L0–L3), which is the correct tuple for the fail-closed branch and would shadow the import anyway. Imported only `TIER_ORDER, load_registry`; behavior identical (verified: parity sets match).
2. **`prescription` import at module level, not lazy (design §2.5.1 vs R5).** Design R5's authoritative text says "import prescription alongside the existing workflow_level import"; module-level is cycle-free (assessor does not import eval) and avoids per-assertion import overhead.
3. **README.md updated** — R6 listed only AGENTS.md/SPEC_SYSTEM, but `README.md:176` carried the identical "both domains" wording that would have become a documented lie.
4. **Failure-attribution detail**: design §1 said the 21 environmental failures are all in `tests/test_pi_batch.py`; live runs show 13 there + **8 in `tests/test_local_fixes.py`** — same `pid=1684832 … Refusing to start (exit 5)` signature, same cause (CLI-subprocess tests hitting the campaign-held repo lock). Zero failures in any other file.

## 3. Self-check

### architecture.md §8 (状态管理)

Not a business-state change (no entities, no transitions) — the closest analogue is the *spec-selection state*, which is centralized in one derivation function with no scattered `if` logic and no direct assignment (registry rules are read-only). The "state machine" of the rule system is the registry's tier/risk/page-type gates, all expressed declaratively in data.

### evolution.md §6 (架构审查清单, 15 items)

| # | Item | Verdict |
|---|---|---|
| 1 | 清晰业务边界 | ✅ product domain owns its registry; matcher routes by domain |
| 2 | 依赖方向单向/无循环 | ✅ lazy import one-directional; smoke import green; comments at both sites |
| 3 | 依赖框架/ORM/SDK | ✅ stdlib + yaml only |
| 4 | 上帝类/文件 | ✅ `quality.py --strict .` → `quality: OK` |
| 5 | 访问其他模块内部 | ✅ product consumes only public `load_registry`/`TIER_ORDER` |
| 6 | DTO/领域/ORM 混用 | ✅ n/a (no ORM) |
| 7 | 单实现无意义接口 | ✅ none added — no pattern, composition suffices |
| 8 | switch/if 表达可变策略 | ✅ the removed hardcoded literal *was* the smell; now data-driven |
| 9 | 状态转换分散 | ✅ n/a — no state machines in this change |
| 10 | 横切逻辑复制 | ✅ extension points are single (domain tuples, eval key tuples) |
| 11 | 第三方 SDK Adapter | ✅ n/a |
| 12 | 事务边界 | ✅ n/a (no DB) |
| 13 | 模块独立测试 | ✅ test_product.py (15), test_rule_matcher.py (28), eval 42 |
| 14 | 改一种实现需改大量调用方 | ✅ `_SPECS_BY_LEVEL` name retained — zero consumer edits |
| 15 | 引入模式未说明 | ✅ no pattern introduced (decision documented §1.6) |

### D5 (stale doc counts) — flagged, not fixed (out of scope)

`AGENTS.md` / `docs/CHECKS_REGISTRY.md` still quote "tests/ 387" and "23/25 eval cases"; the live suite is **978 tests / 42 eval cases**. Flagged per design §1.2 D5; the requirements doc itself said to flag only.

## 4. Verification

### Commands actually run (with results)

| Command | Result |
|---|---|
| `python -m pytest tests/` (×3 runs) | **21 failed / 960 passed / 1 skipped** — all 21 are exit-5 lock failures (13 `test_pi_batch.py` + 8 `test_local_fixes.py`; `pid=1684832 … Refusing to start (exit 5)`); **zero failures in any other file**; 960 = 956 baseline + 4 new tests |
| `python -m pytest tests/test_product.py -q` | 15 passed (13 + 2 new pins) |
| `python -m pytest tests/test_rule_matcher.py -q` | 28 passed (26 + 2 new pins) |
| `python -m pbatch eval` | **EVAL: all 42 passed** (41 + 1 new) |
| `python -m pbatch eval --filter l3-prescription-no-billing-without-signal --json` | `{"total": 1, "failed": 0, "failures": []}` |
| `python -m pbatch rules --check` | 3 × `registry: OK` (frontend_ui/backend/product), exit 0 |
| `mv product-specs/rules.yaml /tmp/ && python -m pbatch rules --check` | `[product] registry empty or missing file; refusing OK`, **exit 1** (F2 closed); restored → exit 0 |
| `python -m pbatch rules "做一个开源上传产品，商业化" --json` | `domain: product`, rules `[product-thinking, commercial-readiness, completion-evidence, open-source-readiness]`, `billing` skipped `page type not matched` (**A3**) |
| `python -m pbatch rules "写个内部小工具脚本，解析日志" --json` | `domain: generic, rules: []` (**F6** L0 restraint) |
| `python -m pbatch rules "做一个 SaaS 计费产品，订阅制收费" --json` | `billing` **loaded** (explicit mechanics double-gate works) |
| `python quality.py --strict .` | `quality: OK` |
| `python -m pbatch check --steps quality,registry,tools` | all 3 passed |
| `python -m pbatch check --steps org,capabilities,graph,knowledge` | all 4 passed |
| `python cli.py check-test` | pass (harness self-test) |
| `python -c "import pbatch.rule_matcher, pbatch.product, pbatch.assessor"` | no import cycle |
| `python -c "…_SPECS_BY_LEVEL…"` | parity: set-equal to pre-change literals for all 4 levels (order drift at L2/L3 cosmetic, unpinned) |
| `python -c "import yaml; …"` on registry | parses; 5 rules |
| `python pi-batch.py assess "<direction>" --json` | prescription loaded (frontend manifest + L3 product specs via `product_manifest` — F7 precedence working) |

### Commands that could not run

- **`make ci` `test` target with zero failures**: the campaign (`pid=1684832, campaign --jobs 4 …`, age >3.7h) holds the repo lock; the 21 environmental failures reproduce on every full-suite run. Re-run `make ci` after campaigns drain — expect 978 tests green (design F9). All lock-free `make ci` components (check incl. registry/eval, self-test, quality, org) are green here.
- No Go or packaging gates are touched by this change (doc/data + Python only).
