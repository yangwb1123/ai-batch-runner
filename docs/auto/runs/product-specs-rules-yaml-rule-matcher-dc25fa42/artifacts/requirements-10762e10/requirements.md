Confirmed: the 21 failures are environmental — a background campaign (`pid=1684832`, `campaign --jobs 4`) holds the repo lock (exit 5 "Another pi-batch instance is running"), blocking CLI integration tests. Not related to this change (which is doc-only). The product-specs-relevant gates all pass: `tests/test_product.py` 13/13, `pi-batch eval` 41/41, `rules --check` clean.

## Deliverable

**`docs/auto/requirements/product-specs-82a451b3-requirements.md`** (333 lines) — evidence-backed requirements specification for the selected direction.

### Key verification findings (every cited symbol checked live)

| Evidence | Result |
|---|---|
| `pbatch/product.py:85` `_SPECS_BY_LEVEL` (85–92), consumed at :140 | ✅ Confirmed |
| `pbatch/rule_matcher.py:42-43` `DOMAIN_REGISTRIES` (backend/frontend_ui) | ✅ Confirmed |
| `rules --check` covers only 2 domains (`rule_cli.py:122`) | ✅ Baseline: `[frontend_ui]` + `[backend]` OK, no product |
| `rules "做一个开源上传产品，商业化" --json` → generic, **0 rules** | ✅ A3 fails today |
| `product-thinking.md` §4/§5/§6 restraint (Billing forbidden without commercial signal) | ✅ Confirmed |

### Critical hazards the spec pins down (found during verification)

1. **Merge-inheritance trap**: `load_registry` resets to empty base only for `("backend", "generic")` (`rule_matcher.py:100`) — adding a product domain without extending this tuple leaks frontend rules (`visual-core`/`component-spec`). Verified by simulation.
2. **Import cycle**: `product.py` will import `load_registry` at module level → `domain_for` must lazy-import `productization_level`.
3. **L0 restraint**: routing happens via `productization_level` (L1+ → product, L0 → generic), keeping classifier untouched and L0 spec-free.

### Design validated by scratch simulation

- Proposed `product-specs/rules.yaml` → 0 schema violations; `commercial-readiness` in manifest for A3 input; `billing` skipped (`page type not matched`); billing **does** load for "做一个 SaaS 计费产品，订阅制收费"
- Derived `_SPECS_BY_LEVEL` (L1→demo, L2→standard, L3→production) reproduces current per-level sets **exactly** for all 4 levels
- `signals: {}` keeps 处方档 at demo — second independent guard so Billing never enters the L3 prescription
- New eval case `l3-prescription-no-billing-without-signal` pins both matcher-level (`lacks_rule`) and prescription-level (`prescription_lacks_rule`, a new eval assertion key) gates to CI

The 4 supplied acceptance checks are preserved and made executable (§4), with regression gates (`eval` 42 cases, `tests/` 387, `quality.py --strict`, `pi-batch check`) and an explicit out-of-scope list (directions #2/#3 of the same analysis).
