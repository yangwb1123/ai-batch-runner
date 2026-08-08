Implementation complete. All gates green.

## Summary

**What was built** (design `product-specs-82a451b3`, campaign `product-specs-rules-yaml-rule-matcher-dc25fa42`):

1. **`product-specs/rules.yaml`** (new) — declarative registry: 5 rules (product-thinking, completion-evidence, commercial-readiness, open-source-readiness, billing), empty `signals` (prescription-level restraint), billing double-gated (`min_tier: production` + `page_types: [billing]`).
2. **`pbatch/rule_matcher.py`** — `DOMAIN_REGISTRIES` + `"product"`; reset tuple + `"product"` (F1 merge-inheritance hazard); `domain_for` lazy-imports `productization_level` (L1+ → product, L0 → generic).
3. **`pbatch/product.py`** — hardcoded `_SPECS_BY_LEVEL` replaced by registry derivation (`_LEVEL_TIERS` + `_derive_specs_by_level()`, fail-closed on missing registry); name retained → zero consumer edits.
4. **`pbatch/rule_cli.py` + `selfcheck.py`** — 三域 `rules --check` + **R4′ non-empty guard** (missing/empty registry → exit 1, closing the vacuous-A1 gap).
5. **`pbatch/eval.py` + `evals/assess.yaml`** — prescription-level assertion keys (3 sites, D4-corrected) + case `l3-prescription-no-billing-without-signal` (pins A3/A4, F1 via `lacks_rule: [visual-core]`, F5 via `prescription_lacks_rule`).
6. **Tests** — 4 new pins: D3 parity (L3 == 4 files, L0 == []), F6 routing, F1 no-frontend-defaults, F2 empty-registry exit 1.
7. **Docs** — SPEC_SYSTEM §2 row + precedence note, AGENTS.md rows + 双域→三域, README sync.

**Verified live**: `rules --check` 3× OK / exit 1 on missing file; A3 (`domain: product`, billing skipped); F6 L0→generic; billing loads only with explicit mechanics; eval **42/42**; `test_product`+`test_rule_matcher` **43 passed**; full suite **960 passed / 21 failed — all exit-5 lock failures** (`pid=1684832` campaign holds the repo lock; 13 in test_pi_batch + 8 in test_local_fixes, zero elsewhere); `quality --strict` OK; all `pi-batch check` gates OK. Full `make ci` test target must be re-run after campaigns drain (F9).

**Flagged deviations** (in report at `docs/auto/runs/…/artifacts/implement-f9bb818d/task-1-implementation.md`): omitted the design's `LEVELS` import (name collision — `rule_matcher.LEVELS` is the rule-level taxonomy, product's own L0–L3 is correct); `prescription` imported at module level per R5's wording; README wording sync; design's "21 failures all in test_pi_batch.py" is actually 13+8 (same signature); stale "387 tests" doc counts flagged, not fixed (D5).
