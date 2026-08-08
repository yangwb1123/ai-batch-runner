All verification complete. Here is my review.

---

# Testing Reviewer Review — product-specs declarative rules.yaml registry (direction 82a451b3)

**Method:** live re-verification of every claim against the current tree (this session, 04:56 UTC). Repo lock is held by 7 live campaign processes (confirmed in `ps`; pid 1684832 matches the design's environmental-failure report), so I ran only lock-free gates per the design's F9 constraint — full suite and `make ci` re-run remains deferred until campaigns drain.

## 1. Verdict

**`VERDICT: FAIL - two blocking test gaps from the previous QA review persist in the live tree`** — (1) the eval case cited as F5 detection is vacuous against its own trigger: a signals escalation in the product registry leaks `commercial-readiness` into the prescription with the case still green (reproduced live below); (2) the R4′ non-empty guard does not detect a deleted `ui-specs/rules.yaml` (frontend defaults mask it, `--check` exits 0). Everything else verifies: eval 42/42, `rules --check` 3× OK, A3/F6 routing, D3 parity, billing double-gate, 60/60 targeted tests, `quality --strict` OK.

## 2. Findings (severity-sorted, all reproduced live this session)

| # | Sev | Defect pattern (testing.md) | Missing test | Exact test that catches it |
|---|---|---|---|---|
| F1 | **High** | Test cannot detect its own stated trigger (assertion-quality) | The eval case `l3-prescription-no-billing-without-signal` claims to pin F5 (signals escalation → prescription leak) but only asserts `prescription_lacks_rule: [billing]` — billing is double-gated at the *matcher* level by `page_types`, which the prescription filter never re-opens | Live repro: inject `signals: {commercial:[商业化], opensource:[开源]}` + weights into the product registry → `scale: M`, `effective_tier: standard`, prescription = `[product-thinking, commercial-readiness, completion-evidence]`; billing stays absent → **case passes = defect**. Fix: add `prescription_lacks_rule: [commercial-readiness]` (and optionally positive `prescription_has_rule: [product-thinking, completion-evidence]`). Verified: with that assertion the same scenario goes **RED** |
| F2 | Medium | Fail-closed guard has a masked branch (partial-success coverage) | `_check_registries_cli` (rule_cli.py) refuses empty registries, but `load_registry(domain="frontend_ui")` starts from `_DEFAULT_REGISTRY` (3 rules) — a deleted/path-typo'd `ui-specs/rules.yaml` yields non-empty → `--check` exit 0. The design's F2 row claims the guard "hardens the existing two domains" — false for frontend | Live repro: `load_registry(path='/nonexistent/rules.yaml', domain='frontend_ui')` → `rules={visual-core, component-spec, business-profile}` (NON-EMPTY, masked); `backend`/`product` → empty → exit 1. Fix: in `_check_registries_cli` require `any(p.exists() for p in _default_registry_paths(domain))` → else exit 1; unit: monkeypatch `_default_registry_paths` to a nonexistent path, assert `SystemExit(1)` for frontend specifically (backend/product already pass today) |
| F3 | Low | Uniqueness constraint weaker than claimed | eval.py:87 enforces non-empty case ids only; no `seen` set → duplicate ids would both run under `--filter` and double-count in totals | Add duplicate-id detection in `load_eval_files` (currently 42/42 unique, so no live impact — hardening only) |
| F4 | Low | Stale claims in deliverable docs (honesty, not silently fixed — correct) | AGENTS.md:67/628 and CHECKS_REGISTRY.md:25 still say "387 个回归测试"; live collection is **982** (verified `--collect-only`). Migration step 8's "expect 978" is also stale (982) | Doc update; D5 already flagged this, no test needed |
| F5 | Low | CI doesn't pin the import-cycle constraint (F3) | `rule_matcher.domain_for` lazy-imports `product`; `product` imports `rule_matcher` at module level — a future module-level import in `rule_matcher` silently breaks the cycle. Smoke-checked manually (`import pbatch.rule_matcher, pbatch.product, pbatch.assessor` OK) but not in `make ci` (Makefile/scripts grep: absent) | Add the one-line import smoke to `make ci` |

## 3. Risk-coverage matrix (testing.md §3 risky paths, mapped to this change)

| Risky path | Covered? | Evidence |
|---|---|---|
| Missing/empty registry → vacuous pass (backend, product) | ✅ | F2 unit (`test_check_registries_fails_closed_on_empty_registry`, monkeypatch — no real-file moves, good fixture hygiene) + live `--check` exit 1 semantics |
| Missing/empty registry (frontend — default masking) | ❌ | Finding 2; live repro shows `--check` OK with registry gone |
| Merge-inheritance (product inherits ui-spec defaults, F1) | ✅ | Reset tuple `("backend","generic","product")` at rule_matcher.py:107 + `test_product_registry_has_no_frontend_defaults` + eval `lacks_rule: [visual-core]` — all green |
| L0 → generic routing (restraint, F6) | ✅ | `test_domain_for_routes_product_and_generic` + live CLI `domain: generic, rules: []` |
| Billing without explicit commercial signal | ✅ | A3 live: 4 rules, billing skipped `page type not matched`; eval case green |
| Billing with explicit mechanics (positive) | ⚠️ | Live CLI only (`订阅制收费` → billing loads); not pinned in evals/ — optional add |
| Signals escalation → prescription leak (F5) | ❌ | Finding 1; live repro: leak occurs, case stays green |
| Registry schema drift (tiers/files/self-reference/suppress_on) | ✅ | `check_registry` + `test_registry_rejects_self_referential_files` + D3 parity pin (`test_derived_specs_parity_l3_and_l0`) |
| Silent spec-set drift (L3==4 files, L0==[]) | ✅ | D3 pin green (set-equality; L2/L3 order differs — declared-order-dependent, unpinned, cosmetic, correctly flagged) |
| Import-cycle reintroduction | ⚠️ | Manual smoke only (Finding 5) |
| Duplicate eval case ids | ❌ | Finding 3 (no `seen` set; no live collision) |
| Async reorder / duplicate submit / timeouts / 409 / partial success / pagination / timezone | N/A | Deterministic in-process registry + pure matcher; no async, no persistence, no concurrency in this change — no such risk surface exists to cover |
| Sleep-based timing control | ✅ | None used; no controllable-clock need (pure functions) |
| Test isolation / execution-order deps | ✅ | No shared mutable state observed in the 60 targeted tests; monkeypatch-scoped |

## 4. Honesty audit (executed vs claimed)

| Claim | This session | Verdict |
|---|---|---|
| eval 42/42 (was 41) | Ran: `python -m pbatch eval` → `all 42 passed` | ✅ Executed |
| Filter case green | Ran: `--filter l3-prescription-no-billing-without-signal --json` → `{"total":1,"failed":0}` | ✅ Executed — **but vacuous against F5 (Finding 1)** |
| `rules --check` 3× OK, exit 0 | Ran: `echo $?` → 0 | ✅ Executed |
| A3 routing (domain product, 4 rules, billing skipped) | Ran: A3 fixture `--json` | ✅ Executed |
| F6 L0→generic, [] | Ran: L0 fixture `--json` | ✅ Executed |
| Billing positive | Ran: SaaS billing fixture | ✅ Executed |
| test_product 15 / test_rule_matcher 28 / test_eval_suite 17 | Ran: 60 passed | ✅ Executed |
| quality --strict | Ran: `quality: OK` | ✅ Executed |
| Import smoke | Ran | ✅ Executed (not in CI — Finding 5) |
| 982 collected (978+4) | Ran: `--collect-only` → 982 | ✅ Executed |
| 21 env-lock failures, exit 5, pid=1684832 | Lock held (7 campaigns live, incl. pid 1684832); `LOCK_EXIT_CODE = 5` at lock.py:51; 982 − 1 skipped = 981 = 960 passed + 21 failed (arithmetic consistent with implementation report) | ⚠️ Not re-run (lock); signature independently confirmed |
| F5 detection via eval case | Repro simulation | ❌ **Claim false — leak undetected** |
| R4′ detects deleted frontend registry | Repro simulation | ❌ **Claim false for frontend** |
| "387 tests" in AGENTS.md/CHECKS_REGISTRY.md | Grepped | ❌ Stale (live 982) — flagged, not silently fixed (correct per completion-evidence) |
| Full `pytest tests/` + `make ci` | Deferred | `not_executed` with reason: repo lock (F9) — must be re-run after campaigns drain; expected 960 passed / 21 exit-5 / 1 skipped |

**No fabricated claims found**: every verification in the implementation report names a command and result; failures are attributed (environmental lock, exit-5 signature) rather than papered over; the two design-claim deviations (LEVELS name collision, 13+8 failure split) were reported, not hidden. The two failing claims are the *pins themselves* (F1/F2 above), which is a test-quality defect, not a fabrication.

**Bottom line:** implementation is functionally correct and heavily pinned where it matters (F1 merge-inheritance, D3 parity, F6 restraint, billing double-gate all live-verified); but the deliverable's own acceptance evidence contains one vacuous pin (F5 — High) and one masked guard branch (frontend deletion — Medium), both one-line fixes with exact tests specified above. Close those two and re-run eval + `rules --check`, then full suite once campaigns drain.
