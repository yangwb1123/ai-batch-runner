All evidence claims re-verified live (`domain: generic, rules: 0` for the A3 fixture; literal at `product.py:85`; reset tuple `("backend","generic")` at `rule_matcher.py:100`; two-domain tuple at `rule_cli.py:122`). Review complete.

---

## Product Thinker Review — product-specs declarative registry (direction 82a451b3)

### 1. Verdict

**`VERDICT: PASS`** — No blocking product-thinking gaps. The task is L0 internal tooling, the design adds no product structure, implements no high-cost capability, and — notably — *is itself the enforcement machinery for the restraint principles* (billing double-gate, empty-`signals` prescription guard, L0→generic routing, `required_when_risk`), with all restraint behaviors verified by live simulation rather than asserted. Four design decisions are implicit requirements needing requester confirmation (Q1–Q4 below), none blocking.

### 2. Productization level + evidence

**Level: L0 `local_feature` (internal tooling)** — overriding the keyword result. `pi-batch assess` on the direction title returned `L3_product_feature` on a single keyword hit (`"product"` substring in "product-specs"), plus `task_type: backend` — both vocabulary false positives. Substance evidence: the deliverable is an internal refactor of the ai-batch-runner's own spec-loading path (`pbatch/product.py`, `rule_matcher.py`, `rule_cli.py`); no user-facing surface, no commercial signal, no external consumer, single repo. Per `product-thinking.md` §2 decision table (内部/utility/script → L0), **no scenario chains fire, no product structure, depth = the requirement itself**.

The design's *content*, however, correctly governs user tasks at all levels: `_LEVEL_TIERS` (L1=demo/L2=standard/L3=production) matches the spec's restraint table; L0 → generic → zero specs; `billing` loads only under explicit commercial-mechanics keywords (live-verified: "商业化" alone → skipped, `page type not matched`). This is the productization framework encoding its own rules correctly — the key quality bar for this deliverable, and it passes.

### 3. Implicit-requirement questions (all **待确认** — requester decides, none implemented unilaterally)

| # | Question | Decision the requester must make |
|---|---|---|
| Q1 | **D1 scope extension**: the non-empty registry guard applies to backend/frontend domains too, not just product — today an emptied/missing registry passes `rules --check` vacuously; after D1 it exits 1 (fail-open→fail-closed flip for the *checker* only) | Accept (recommended — closes the vacuous-A1 gap and hardens existing domains, aligns with fail-closed philosophy) or scope the guard to product only |
| Q2 | **D1 × packaging deferral**: registries ship repo/CWD-relative, not in the wheel; after D1 a pip-installed, non-repo `pi-batch check` registry step will **fail** (missing files → exit 1), where today it passes vacuously | Accept (repo-root workflow assumption, consistent with ui/backend) or ship `product-specs/rules.yaml` in `pyproject.toml` package-data |
| Q3 | **D3 parity pin REQUIRED**: derived L3 spec set frozen == exactly the 4 product-spec files; future spec growth requires deliberate dual update (registry + pin) | Accept (recommended — silent set drift is worse than a deliberate gate) or demote to optional |
| Q4 | **F7 precedence**: classifier wins over product signals — e.g. "做一个 SaaS 计费前端页面" routes to `frontend_ui`, getting no product rules at matcher level (product specs still reach it via `assess`'s `product_manifest`) | Accept the documented precedence, or escalate product signals above classifier routing (not recommended — would churn routing) |
| Q5 | **Billing signal vocabulary**: `配额/quota` in `use_case_types.billing` — quota features are often internal rate-limiting without billing intent; mitigated by the double gate (`min_tier: production` + empty `signals` prescription guard) but the keyword itself is greedy | Accept (current double-gate suffices) or defer to direction #2's keyword tightening (design already defers) |

### 4. Level-matched checks

**Low-cost reservations — applicable ones present**: no tenant/data-model surface exists for this task (internal tool), so `tenant_id`/audit/versioning reservations are N/A. The applicable reservation is **API/signature stability**, and it is fully preserved: no signature changes (`domain_for`, `load_registry`, `match_rules`, `prescription`, `rules_main` untouched), `_SPECS_BY_LEVEL` name retained so all consumers (`assessor.py:41`, `profile.py:29`, `eval.py:49`) need zero edits, `load_registry` fail-open contract preserved, eval suite strictly additive (41→42). ✅

**High-cost implementations — none, and correctly gated**: no Billing/Subscription/marketplace/license-tier *implementation* anywhere. The `billing` rule is a *restraint gate* (triple-gated: `min_tier: production` + `page_types: [billing]` + empty-`signals` prescription guard) — "先测量，后收费" encoded as code, verified live at both matcher level (A3) and prescription level (A4). ✅

**Open-source readiness — not applicable, correctly skipped**: the task creates no new external/open-source surface (the repo is already open); no README/LICENSE/CHANGELOG/CI suite generated for an L0 internal change — restraint per §4 "无开源意图不加载". ✅

### 5. Restraint notes (deliberately NOT done)

- **No product structure** for an L0 task — no packages/enterprise/plugins dirs, no new abstraction: the registry is the *third* consumer of the established ui/backend contract (三次原则 satisfied by existing pattern, not by speculative generality).
- **No Billing design**: the high-cost rule exists solely to keep itself and its file out of every prescription unless commercial mechanics are explicit — the design *prevents* over-productization rather than adding it.
- **No scope creep into sibling directions**: scenario keyword tightening (#2) and execution-prompt spec injection (#3) are explicitly out of scope even though this registry is their prerequisite — the design resists the "while we're here" trap.
- **L0 tasks never reach the product domain** — `domain_for` returns `generic` for L0 (verified in design + pin F6); restraint is *built into the routing*, not left to prompt discipline.
- **Fail-closed derivation**: missing/empty registry → `[]` + warning, never a hardcoded fallback — prevents silent over-specification if the registry degrades.
- **Honest reporting**: stale "387→978" test counts flagged (D5) and *not* silently fixed; precedence surprise (F7) documented rather than auto-corrected; env-lock failures (21, pid=1684832) correctly attributed as environmental, not papered over.

**Remaining evidence gaps (non-blocking)**: Q2's installed-CLI behavior is reasoned, not executed (a pip-install smoke test would settle it); Q5's "配额" false-positive rate is unmeasured (a negative eval case is direction #2 territory). Both are marked 待确认, not silently implemented.
