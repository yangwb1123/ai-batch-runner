All three eval sites confirmed in the live implementation. The design is fully implemented and passing in this tree — I can now deliver the review.

---

# QA Lead Review — product-specs declarative rules.yaml registry (direction 82a451b3)

**Review method:** risk-based test review of design + live implementation. All claims below re-run against the current tree during this session, not taken from the design. **Note: the design was implemented concurrently while I reviewed** — `product-specs/rules.yaml`, `evals/assess.yaml` case, and the `pbatch` changes landed mid-session; I re-verified everything after they did, so this review covers the *live implemented state*, which is what matters.

## 1. Test inventory and commands actually run (this revision)

| Command | Result |
|---|---|
| `python -m pbatch eval` | ✅ `all 42 passed` (was 41 before the case landed mid-session) |
| `python -m pbatch eval --filter l3-prescription-no-billing-without-signal --json` | ✅ `{"total":1,"failed":0,"failures":[]}` |
| `python -m pbatch rules --check; echo $?` | ✅ `[frontend_ui]` / `[backend]` / `[product] registry: OK`, exit 0 |
| `python -m pbatch rules "做一个开源上传产品，商业化" --json` | ✅ `domain: product`; rules = product-thinking, commercial-readiness, completion-evidence, open-source-readiness; `billing` skipped `page type not matched` |
| `python -m pbatch rules "写个内部小工具脚本，解析日志" --json` | ✅ `domain: generic, rules: []` (F6) |
| `python -m pbatch rules "做一个 SaaS 计费产品，订阅制收费" --json` | ✅ `billing` loads when mechanics explicit |
| `python -m pytest tests/test_product.py -q` | ✅ 15 passed (13 baseline + D3 parity + F6 routing pins) |
| `python -m pytest tests/test_rule_matcher.py -q` | ✅ 28 passed (incl. F1 no-frontend-defaults pin, F2 fail-closed guard) |
| `python -m pytest tests/test_eval_suite.py -q` | ✅ 17 passed |
| `python -m pytest tests/test_packaging.py -q` | ✅ 6 passed, 1 skipped (deferral unchanged) |
| `python -m pytest tests/ --collect-only -q` | ✅ 982 collected (978 baseline + 4 new) |
| `python quality.py --strict .` | ✅ `quality: OK` |
| `python -c "import pbatch.rule_matcher, pbatch.product, pbatch.assessor"` | ✅ import-cycle smoke OK |
| Simulation: F1 unfixed/fixed, vacuous-A1, R4′ frontend deletion, F5 signals escalation | see §3 |

**Not run:** full `pytest tests/` (982) and `make ci` — two live campaign processes hold the repo lock (`ps`: pids 204258/225404); the design's 21-environmental-failure claim was independently confirmed at component level (`pbatch/lock.py:51` `LOCK_EXIT_CODE = 5`, exact message text at :167-169) and the design reproduced it with matching output. All gates I ran are lock-free per the design's F9 constraint.

## 2. Requirement-to-test matrix

| Req | Test/assertion | Status | Evidence (live) |
|---|---|---|---|
| R1 registry file | A1: `rules --check` exit 0 + `[product] registry: OK`; content check (6 sections, `signals: {}`, 5 rules with correct tiers/files/gates) | ✅ Verified | exit 0, 3× OK; YAML parsed, normative content matches, all referenced files exist |
| R1 non-vacuous (D1) | F2 guard: empty registry → exit 1 (`test_check_registries_fails_closed_on_empty_registry`) | ✅ Verified | guard in `_check_registries_cli`; unit passes. **Gap for frontend deletion — see Finding 2** |
| R2 domain mapping | A3: `domain: product`, commercial-readiness in rules, billing skipped | ✅ Verified | live CLI output matches exactly |
| R2 reset tuple (F1) | `test_product_registry_has_no_frontend_defaults` (∌ visual-core/component-spec/business-profile) + eval `lacks_rule: [visual-core]` | ✅ Verified | both pass; direct `load_registry(domain="product")` clean |
| R2 L0 restraint (F6) | `test_domain_for_routes_product_and_generic` + CLI generic/[] | ✅ Verified | unit + live CLI |
| R2 precedence (F7) | same test: backend/frontend routing untouched | ✅ Verified | `backend`/`frontend_ui` assertions pass |
| R3 derivation | A2: `test_product.py` 15/15 incl. `test_derived_specs_parity_l3_and_l0`; set-equality live | ✅ Verified | derived L0=[], L1/L2/L3 set-equal; L1 order identical, L2/L3 order differs (unpinned, cosmetic) |
| R4 three-domain check | A1 live CLI | ✅ Verified | 3 registry OK lines |
| R5 eval keys | A4: eval 42/42; keys at eval.py:21-22 (docstring), :118 (dispatch), :153/:157 (message branches), :180 (run_suite tuple) | ✅ Verified | all 4 sites present; case green |
| R5 case | A4: `l3-prescription-no-billing-without-signal` asserts L3 / has_rule / lacks_rule / prescription_lacks_rule | ✅ Verified | filter run `{"total":1,"failed":0}`. **Prescription assertion too weak — see Finding 1** |
| R6 docs | AGENTS.md, SPEC_SYSTEM.md, selfcheck.py wording | ✅ Partial | files modified in git status; wording not exhaustively diffed |
| Regression gates | eval 42, tests 982 (was 978 — see Finding 4), quality strict, packaging | ✅ Verified | all run green except full-suite/`make ci` (lock-held, F9) |

## 3. Findings (severity-sorted)

**FINDING 1 — High: the F5 prescription assertion cannot detect its own stated trigger.**
- **Untested failure:** a future `signals`/`signal_weights` escalation in the product registry (F5's exact trigger) leaks `commercial-readiness` into the prescription — but the eval case only pins `prescription_lacks_rule: [billing]`, which stays green.
- **Evidence (live simulation, real `prescription()`):** adding `signals: {commercial: [商业化], opensource: [开源]}` → `scale_signal` M → effective tier `standard` → prescription = `[product-thinking, commercial-readiness, completion-evidence]`. `billing` remains absent (it is double-gated *at the matcher level* by `page_types`, which the prescription filter never re-opens). So the A4 "处方档不加载高成本实现" guarantee and the design's F5 detection row are both unpinned by the case as written.
- **Regression risk:** the empty-signals restraint (product-thinking.md §5) silently degrades with no red test.
- **Exact test to add:** extend the case: `prescription_lacks_rule: [billing, commercial-readiness]` (add `open-source-readiness` for the L-escalation case), and optionally a positive `prescription_has_rule: [product-thinking, completion-evidence]` to pin the prescription staying non-empty.
- **Acceptance assertion:** `python -m pbatch eval --filter l3-prescription-no-billing-without-signal --json` → `{"total":1,"failed":0}` *after* the signal escalation is applied — currently passes, which is the defect.

**FINDING 2 — Medium: R4′ does not detect a deleted `ui-specs/rules.yaml` (frontend masking).**
- **Untested failure:** `load_registry(domain="frontend_ui")` starts from `_DEFAULT_REGISTRY` (visual-core/component-spec/business-profile); a deleted or path-typo'd frontend registry yields those defaults → non-empty → `rules --check` exits 0. Verified by simulation (`frontend rules w/ deleted file: [business-profile, component-spec, visual-core]`; `backend`/`product` correctly → empty → exit 1).
- **Evidence:** the design's F2 row claims the guard "hardens the existing two domains against silently deleted files" — false for frontend; the added unit test monkeypatches `load_registry` to `{"rules": {}}` and tests the guard branch only, not file-missing detection.
- **Exact test to add:** in `_check_registries_cli`, require `any(p.exists() for p in _default_registry_paths(domain))` → else exit 1; unit: monkeypatch `_default_registry_paths` to a nonexistent path and assert `SystemExit(1)` for the frontend domain specifically.
- **Acceptance assertion:** with `ui-specs/rules.yaml` moved aside, `python -m pbatch rules --check; echo $?` → 1 (currently 0).

**FINDING 3 — Low: "case-id uniqueness enforced at :87" is inaccurate.**
- eval.py enforces **non-empty** ids only (~:83-88); no duplicate detection. Currently 42/42 unique, no collision risk (`--filter` would run both duplicates). Fix the design text or add a `seen` set in `load_eval_files`.

**FINDING 4 — Low: two stale numbers in the design itself.**
- "Order differs at L2/L3 only" is declaration-order-dependent: with alphabetical rule ordering (e.g. editor auto-sort) L1 order differs too (verified). No test pins order (D3 correctly pins set-equality), so cosmetic — but the claim should not imply a stable property.
- Migration step 8's "expect 978" is already stale: live collection is **982** (978 + 4 new pins; the "387" in AGENTS.md/requirements is stale by 595 — D5 correctly flagged).

**FINDING 5 — Info: import-time I/O.** `_derive_specs_by_level()` reads `product-specs/rules.yaml` at every `import pbatch.assessor` (assess/eval/profile/cli). Fail-open + warning confirmed; acceptable, but a missing registry now silently empties product specs at process start — the R4′ checker is the only tripwire. Consistent with the documented fail-open contract.

## 4. Prioritized scenario list (for the implemented change)

1. **Happy (L3 product task):** "做一个开源上传产品，商业化" → domain product, production tier, 4 rules, billing skipped — **covered** (eval + CLI, verified).
2. **Happy (explicit mechanics):** "做一个 SaaS 计费产品，订阅制收费" → billing loads — **covered** (CLI, verified); **not pinned in evals/** — recommend adding a positive eval case or accepting CLI-only coverage.
3. **Boundary (L0 restraint):** "写个内部小工具脚本，解析日志" → generic, [] — **covered** (unit + CLI).
4. **Boundary (precedence, F7):** product-signal task classifying backend/frontend ("做一个 SaaS 计费前端页面") → frontend registry wins; `product_manifest` still level-driven via assess — **covered** by unit precedence assertions; no eval case — acceptable (documented decision).
5. **Error (F2):** missing/empty registry → exit 1 — **covered** for backend/product; **frontend gap (Finding 2)**.
6. **Error (import cycle, F3):** module-level import reintroduced in `rule_matcher` — **covered** by import smoke; not in CI — recommend adding `python -c "import pbatch.rule_matcher, pbatch.product, pbatch.assessor"` to `make ci`.
7. **Race (F9):** full suite during campaign lock → 21 exit-5 failures — documented; all targeted gates lock-free — verified.
8. **Recovery/drift (F4):** registry gains a rule with `min_tier ≤ L3` + new file → parity pin (D3) goes red — **covered** (`test_derived_specs_parity_l3_and_l0`).
9. **Restraint regression (F5):** signals escalation → prescription leak — **NOT covered (Finding 1)** — highest-priority add.

## 5. CI/manual-suite gaps, flake risks, fixtures, exit criteria

- **Gaps:** F5 sensitivity (Finding 1, one-line fix); frontend file-deletion detection (Finding 2); billing-positive eval pin (optional); import smoke in `make ci` (cheap).
- **Flake risks:** none observed in 40+ runs of the targeted gates; the only flake source is the environmental repo lock (F9), which the design's lock-free gate selection correctly avoids. The F2 test uses monkeypatch (no real-file moves) — good fixture hygiene; keep it that way.
- **Fixtures needed:** none beyond the registry itself; do not add tests that move/delete real registry files (would race concurrent campaigns and poison `_derive_specs_by_level` at import time in the same pytest session).
- **Exit criteria (verified green this revision):** eval 42/42 incl. filter case; `rules --check` 3 domains exit 0; test_product 15/15; test_rule_matcher 28/28; quality strict OK; packaging unchanged; import smoke OK. Full `pytest tests/` (982) + `make ci` must be re-run once campaigns drain (lock-free) — expected 960 passed / 21 env-lock failed / 1 skipped, with failures attributable only by the exit-5 signature.

**Verdict:** design and implementation are sound and well-pinned overall — the risk-based review found the F1 merge-inheritance hazard, vacuous-A1, D3 parity, and L0 restraint all correctly converted into executable pins, and every acceptance criterion A1–A4 verifies live. One **required** correction (Finding 1 — the F5 prescription assertion is vacuous against its own trigger) and one recommended hardening (Finding 2) before this counts as closed; both are one-line changes with exact tests specified above.
