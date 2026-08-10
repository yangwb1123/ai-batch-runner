# Design — Declarative rules.yaml registry for product-specs + rule_matcher domain mapping

| Field | Value |
|---|---|
| Module | `product-specs` |
| Direction | `82a451b3` (direction #1 of `docs/auto/analyses/product-specs-82a451b3.json`) |
| Requirements | `docs/auto/requirements/product-specs-82a451b3-requirements.md` (R1–R6) |
| Status | Design (evidence re-verified live; two doc discrepancies flagged, §1.3) |

---

## 1. Verification report (evidence re-checked against live source)

All claims in the supplied evidence and the requirements document were
re-verified in this repository on the current tree. `python -m pytest
tests/` was executed (110s) — the "21 environmental failures" claim was
reproduced **exactly**: `21 failed, 956 passed, 1 skipped`. The 21 failures
are all CLI integration tests in `tests/test_pi_batch.py` whose subprocess
hits the repo lock; the failure payload quotes the holder verbatim:
`Another pi-batch instance is running (pid=1684832, host=u1-FAEX9,
argv=pi-batch.py campaign --jobs 4 ...). Refusing to start (exit 5)`
(`pbatch/lock.py:167`, `LOCK_EXIT_CODE = 5`; live campaigns confirmed in
`ps` at verification time). No product-specs- or rule-matcher-relevant test
is among the failures — the change under design is doc/data-only and does
not touch lock acquisition.

### 1.1 Claim-by-claim verdict

| Evidence claim | Verdict | Live check |
|---|---|---|
| 21 failures environmental; pid=1684832 campaign holds repo lock; exit 5 | ✅ reproduced | full suite run → 21 failed / 956 passed / 1 skipped; error text matches exactly |
| `tests/test_product.py` 13/13 | ✅ | `13 passed in 0.27s` |
| `pi-batch eval` 41/41 | ✅ | `EVAL: all 41 passed` (assess 6 + classifier 18 + rules 17 cases) |
| `rules --check` covers only 2 domains | ✅ | `[frontend_ui] registry: OK`, `[backend] registry: OK`, exit 0; `domains = ("frontend_ui", "backend")` at `rule_cli.py:122`; "双域" help at `:153`; selfcheck comment at `selfcheck.py:5` ("both domain rule registries") |
| `rules "做一个开源上传产品，商业化" --json` → generic, 0 rules (A3 fails today) | ✅ | live: `domain: generic, tier: standard, rules: [], skipped: []` |
| `product.py:85` `_SPECS_BY_LEVEL` (85–92), consumed at `:140` | ✅ | exact lines; dict spans 85–92; `"specs": _SPECS_BY_LEVEL.get(resolved, [])` at 140 |
| `rule_matcher.py:42-43` `DOMAIN_REGISTRIES` | ✅ | lines 41–45; `"backend"` at 42, `"frontend_ui"` at 43 |
| Merge-inheritance trap: reset to empty base only for `("backend","generic")` | ✅ | `rule_matcher.py:100` `if domain in ("backend", "generic"):`; simulation logic sound — `merged` starts from a copy of `_DEFAULT_REGISTRY` (which contains `visual-core`/`component-spec`/`business-profile`), only the tuple empties it |
| Import cycle: `product.py` imports `load_registry` at module level → `domain_for` must lazy-import `productization_level` | ✅ | `rule_matcher` module-level imports (classifier/config/relevance/text_io/system_type_methodology) contain **no** `product`; `product.py` imports only `.relevance`; `assessor.py:38/41` imports both already — cycle-free only with the lazy import |
| L0 restraint via `productization_level`, classifier untouched | ✅ | `domain_for` (`:78-86`) returns `DEFAULT_DOMAIN` ("generic") for unknown task types; L0 → stays generic |
| `product-thinking.md` §4/§5/§6 restraint | ✅ | §4 "无开源意图时不加载" (:43/:49); §5 "高成本实现（有明确商业信号才做，禁止提前设计）：完整 Billing/Subscription…" (:59–60); §6 "无商业化信号不设计 Billing" (:67) |
| `signals: {}` → assess effective tier stays demo (second guard) | ✅ | `scale_signal` (`assessor.py:91`): no signal hits → score 0 → `"S"`; `_SCALE_TIERS["S"] = "demo"` → `prescription` keeps only tier ≤ demo rules |
| Proposed registry → 0 schema violations | ✅ | `_check_rule` (`rule_cli.py:76-99`) only validates `description`/`min_tier`/`level`/`suppress_on`/`files` — extra keys (`risk`, `page_types`, `required_when_risk`) are tolerated; all 5 referenced files exist |
| `billing` loads for "做一个 SaaS 计费产品，订阅制收费", skipped for "商业化"-only | ✅ | matcher path: `case_section = registry.get("use_case_types") or ...`; `billing` use_case_type keywords (订阅/计费/定价/账单/套餐/配额) absent from the A3 input → `page type not matched`; `min_tier: production` is the second gate |
| Derived `_SPECS_BY_LEVEL` reproduces current sets exactly | ✅ (set-equality) | re-derived independently + **live simulation** (§5 scratch): L1→demo {product-thinking, completion-evidence}, L2→standard {+commercial-readiness}, L3→production {+open-source-readiness}; `billing` (min_tier production) qualifies at L3 but its file duplicates commercial-readiness.md → dedup keeps the 4 current files; **set-equality holds for all 4 levels**; order differs at L2 and L3 (completion-evidence moves earlier) — **no test pins order** (confirmed: `tests/test_product.py` asserts L0 == `[]` and membership only) |
| eval machinery extendable | ✅ | `_assert_value` at `eval.py:101`; set-semantics branches at `:147/:153` (`_list_assertion_failure`) and tuple at `:173` (`run_suite`); `--filter` at `:235`; case-id uniqueness enforced at `:87`; `prescription` exists at `assessor.py:150` returning `{"prescription": [items-with-id]}`; eval already imports `.assessor` (`:46`) |
| Packaging: only `pbatch/*.yaml` shipped; registries repo/CWD-relative | ✅ | `pyproject.toml:34` `[tool.setuptools.package-data]`; `_default_registry_paths` resolves `Path(__file__).parent.parent / rel` + CWD-relative |

### 1.2 Gaps found in the requirements document (design deltas)

1. **D1 — `rules --check` with a missing registry file passes silently.**
   `load_registry` swallows read/YAML errors (`except Exception: continue`)
   and `check_registry` iterates `(registry.get("rules") or {}).items()` —
   an **empty rules section yields zero violations**. Today this means A1
   ("`rules --check` 通过（含 product 域 schema 校验）") would pass even if
   `product-specs/rules.yaml` were deleted or its path typo'd in
   `DOMAIN_REGISTRIES`. Same latent gap exists for backend/frontend. The
   design adds a non-empty guard (§3 R4′).
2. **D2 — The new eval case does not pin the merge-inheritance hazard.**
   `has_rule: [product-thinking, commercial-readiness]` asserts presence;
   nothing asserts absence of inherited frontend defaults. Adding
   `lacks_rule: [visual-core]` pins `rule_matcher.py:100` to CI.
3. **D3 — Parity pin must be REQUIRED, not optional.** The derivation rule
   "collect files of every rule with `min_tier ≤ level_tier`" is
   order/dedup-sensitive; without the pin, a future low-tier rule pointing
   at a new file silently grows the L3 spec set (see failure mode F4).
4. **D4 — Line citations drift:** `eval.py` `_assert_value` is at `:101`
   (doc says 104–122) and the "set-semantics tuple" is not a single tuple —
   it is three sites: dispatch `:101`, message branches `:147/:153`, and
   the `run_suite` tuple `:173` (doc says :138). R5 must touch all three.
5. **D5 — Stale test count.** AGENTS.md / `docs/CHECKS_REGISTRY.md` /
   requirements §4 quote "tests/ 387"; the live suite is **978 cases** (971
   `def test_` + parametrization), `make test` = 978 collected. Acceptance
   numbers in this design use the live count; fixing the doc counts is out
   of scope (flag only).

### 1.3 Design simulation (live, monkeypatched, zero repo changes)

Scratch run against the live matcher with the proposed registry
(`/tmp/product-rules.yaml`, normative content of R1) and the fixed empty-
base behavior:

- **F1 hazard reproduced:** current `load_registry(domain="product")` (reset
  tuple unfixed) yields rules `[billing, business-profile,
  commercial-readiness, completion-evidence, component-spec,
  open-source-readiness, product-thinking, visual-core]` — frontend
  defaults leak. With the reset tuple fixed: clean `[billing,
  commercial-readiness, completion-evidence, open-source-readiness,
  product-thinking]`.
- **Derivation** (R3): L0→`[]`; L1→`[product-thinking.md,
  completion-evidence.md]`; L2→`[+commercial-readiness.md]`;
  L3→`[+open-source-readiness.md]` — set-equal to the current literal for
  all four levels (order differs at L2/L3 only).
- **A3 path:** `match_rules("做一个开源上传产品，商业化", product registry)` →
  tier `production`, rules `[product-thinking, commercial-readiness,
  completion-evidence, open-source-readiness]`, `billing` skipped with
  `page type not matched`.
- **A4 path:** `prescription(...)` → `scale=S, effective=demo`; prescription
  = `[product-thinking, completion-evidence]` — billing excluded by the
  empty-`signals` guard.
- **Billing loads when mechanics are explicit:** "做一个 SaaS 计费产品，订阅制收费"
  → `billing` in rules (not skipped).
- **L0 restraint:** "写个内部小工具脚本，解析日志" → `productization_level` L0,
  `domain_for` → generic (current code already routes it so; the design
  preserves this).

---

## 2. API changes

### 2.1 `pbatch/rule_matcher.py`

1. `DOMAIN_REGISTRIES` (:41-45) — add entry:
   ```python
   DOMAIN_REGISTRIES = {
       "backend": ["backend-specs/rules.yaml"],
       "frontend_ui": ["ui-specs/rules.yaml"],
       "product": ["product-specs/rules.yaml"],
       "generic": [],
   }
   ```
2. `load_registry` reset tuple (:100) — **mandatory**, else product
   inherits `_DEFAULT_REGISTRY` frontend rules (verified hazard):
   ```python
   if domain in ("backend", "generic", "product"):
   ```
3. `domain_for` (:78-86) — product routing after backend/frontend checks
   (precedence preserved), with **lazy import** (cycle constraint):
   ```python
   def domain_for(text, classification=None):
       cls = classification or classify_text(text)
       if cls.task_type == "backend":
           return "backend"
       if cls.task_type == "frontend_ui":
           return "frontend_ui"
       from .product import productization_level   # lazy: avoids import cycle
       if productization_level(text)[0] != "L0_local_feature":
           return "product"
       return DEFAULT_DOMAIN
   ```
   Behavior: L1/L2/L3 productization → `product`; L0 (小工具/脚本/内部/…) →
   `generic` (restraint). Signature unchanged; all existing callers
   (`match_rules`, `prescription`, `summarize_task`, `assessor`) unaffected.

### 2.2 `pbatch/product.py`

1. Module-level imports (new): `from .rule_matcher import LEVELS, TIER_ORDER, load_registry`
   (`LEVELS` is used by the derivation's fail-closed branch; `log` comes
   from `.config`) — safe: `rule_matcher` never imports `product` at module
   level (lazy import is one-directional).
2. Delete the `_SPECS_BY_LEVEL` literal (:85-92); derive at module level:
   ```python
   _LEVEL_TIERS = {"L1_reusable_module": "demo", "L2_platform_capability": "standard",
                   "L3_product_feature": "production"}   # L0 → no tier → []
   def _derive_specs_by_level() -> dict:
       """Derive per-level spec lists from the product registry. Fail
       closed: missing/empty registry → [] per level + warning (never fall
       back to hardcoded lists)."""
       reg = load_registry(domain="product")
       if not reg.get("rules"):
           log.warning("product registry missing/empty; product specs empty")
           return {level: [] for level in LEVELS}
       derived = {"L0_local_feature": []}
       for level, tier in _LEVEL_TIERS.items():
           rank = TIER_ORDER[tier]
           files, seen = [], set()
           for rule in reg["rules"].values():
               if TIER_ORDER.get(rule.get("min_tier", "standard"), 1) <= rank:
                   for f in rule.get("files", []):
                       if f not in seen:
                           seen.add(f); files.append(f)
           derived[level] = files
       return derived
   _SPECS_BY_LEVEL = _derive_specs_by_level()
   ```
   The `_SPECS_BY_LEVEL` name is **retained** so `product_manifest` (:140)
   and all consumers (`assessor.py:41`, `profile.py:29`, `eval.py:49`) are
   source-compatible with zero edits. Importing `log` from `.config` is
   added. Verified: this derivation reproduces the current 4 sets exactly
   (dedup in registry declaration order; `billing` contributes a duplicate
   file at L3 only).

### 2.3 `pbatch/rule_cli.py`

1. `_check_registries_cli` (:116-133): `domains = ("frontend_ui", "backend", "product")`.
2. **R4′ (design delta D1)** — fail closed on empty registries:
   ```python
   for domain in domains:
       registry = load_registry(domain=domain)
       if not registry.get("rules"):
           log.error("[%s] registry empty or missing file; refusing OK", domain)
           sys.exit(1)
       ...
   ```
   This closes the vacuous-A1 gap (and, as a side effect, hardens the
   existing two domains against silently deleted files). Check-only
   behavior change: a deliberately emptied registry now exits 1 instead of
   reporting OK.
3. Wording: docstring (:119-121) "both domain registries" → three;
   `--check` help (:153) "双域" → "三域（frontend/backend/product）".

### 2.4 `pbatch/selfcheck.py`

Comment at :5 "both domain rule registries" → three. No code change — the
`registry` step (:37) invokes `rules --check` and inherits the extension.

### 2.5 `pbatch/eval.py` — prescription-level assertion keys (R5, D4-corrected)

Three sites, not one:

1. `_assert_value` (:101) — new dispatch branch:
   ```python
   if key in ("prescription_has_rule", "prescription_lacks_rule"):
       from .assessor import prescription   # extend existing import at :46
       return {item["id"] for item in prescription(input_text)["prescription"]}
   ```
2. `_list_assertion_failure` (:147/:153) — extend both branches:
   `("has_rule", "includes", "prescription_has_rule")` and
   `("lacks_rule", "excludes", "prescription_lacks_rule")`.
3. `run_suite` tuple (:173): `("has_rule", "includes", "lacks_rule",
   "excludes", "prescription_has_rule", "prescription_lacks_rule")`.
4. Module docstring assertion-key list (:17-28 region): add the two keys.
   `EXECUTABLE_EXPECT_KEYS` (:59) stays unchanged — the new keys are
   `assert:`-only (expect-style execution is not meaningful for them).

### 2.6 New files

- `product-specs/rules.yaml` — normative content as specified in R1 of the
  requirements document (scale / signals `{}` / signal_weights `{}` /
  use_case_types / risk / rules: product-thinking, completion-evidence,
  commercial-readiness, open-source-readiness, billing). Registry file is
  loaded repo-root- and CWD-relative by `_default_registry_paths` — **no
  packaging change** (documented deferral, `tests/test_packaging.py`).
- `evals/assess.yaml` — new case (R5 + D2 hardening):
  ```yaml
  - id: l3-prescription-no-billing-without-signal
    input: "做一个开源上传产品，商业化"
    assert:
      product_level: L3_product_feature
      has_rule: [product-thinking, commercial-readiness]
      lacks_rule: [billing, visual-core]
      prescription_lacks_rule: [billing]
  ```
  `lacks_rule: [visual-core]` pins the `load_registry` reset tuple (D2);
  `prescription_lacks_rule` pins the empty-signals guard; `has_rule`
  pins A3.

---

## 3. Compatibility constraints

| Constraint | Detail |
|---|---|
| No signature changes | `domain_for`, `load_registry`, `match_rules`, `product_manifest`, `prescription`, `rules_main` signatures untouched. `_SPECS_BY_LEVEL` name retained (derived value). |
| No classifier changes | `classify_text` / `task_keywords.yaml` / pipeline routing untouched. Product domain is selected only inside `domain_for` for non-backend/non-frontend tasks. |
| Import graph | One-directional lazy import only: `rule_matcher.domain_for` lazy-imports `product` at call time; `product` imports `rule_matcher` at module level. A future module-level `product` import in `rule_matcher` reintroduces a cycle — guard with a comment at both sites. |
| `load_registry` fail-open contract preserved | Missing/corrupt registry still degrades (now to an empty product ruleset), consistent with backend behavior; the *checker* (R4′) is what fails closed. |
| `product_manifest` output identical | Derived spec sets == literal sets for all 4 levels (set-equality, verified by live simulation); order drift at L2/L3 is cosmetic and unpinned by tests (D3 adds the set-equality pin) |
| Eval suite backward compatible | 41 existing cases untouched; only additions. New keys are assert-only and fail closed (`ValueError` → case failure) if misused. |
| `rules --check` exit semantics | 0 = all three registries valid **and** non-empty; 1 = any violation. Third log line `[product] registry: OK`. `pi-batch check` registry step inherits automatically. |
| Behavior precedence note | Product-signal tasks that classify as backend/frontend_ui get the backend/frontend registry, **not** product rules, at matcher level (design decision R2, precedence preserved). Product specs still reach such tasks via `product_manifest` in `assess` (level-driven, domain-independent). Document this in `docs/SPEC_SYSTEM.md` R6 row. |
| `risk:` rule key is metadata-only | `_rule_skip_reason` reads `required_when_risk`, not `risk`. The proposed `commercial-readiness` entry's `risk: [high]` documents intent; behavior is driven by `required_when_risk: true`. Keep both in the registry (declarative) — this matches the existing backend registry convention. |
| Packaging / installs | No wheel change; `product-specs/rules.yaml` resolved repo-root/CWD-relative like ui/backend registries (`tests/test_packaging.py` deferral unchanged). |
| Concurrency | Implementation/verification must not acquire the repo lock (campaigns run 7×24; the 21 lock failures are environmental). Use lock-free gates (`rules`, `eval`, `pytest tests/test_product.py`) locally; full suite after campaigns drain or with `--no-lock` discipline. |

---

## 4. Failure modes

| # | Failure mode | Trigger | Detection / mitigation |
|---|---|---|---|
| F1 | **Frontend default-rule leakage into product manifests** | `load_registry` reset tuple not extended (the requirements doc's verified hazard) | `lacks_rule: [visual-core]` in the new eval case (D2) → `eval` red; plus optional unit pin `load_registry(domain="product")["rules"]` ⊇ product ids, ∌ `visual-core` |
| F2 | **Vacuous A1**: `rules --check` OK with missing/corrupt registry file | typo in `DOMAIN_REGISTRIES` path, file deleted, bad YAML swallowed by `load_registry` | R4′ non-empty guard → exit 1; new unit test deleting/emptying the file asserts exit 1 |
| F3 | **Import cycle** at interpreter startup | someone adds module-level `from .product import ...` to `rule_matcher` | Both import sites carry comments; `python -c "import pbatch.rule_matcher, pbatch.product, pbatch.assessor"` smoke test in CI |
| F4 | **Silent level-spec drift**: a future rule with `min_tier ≤ L3` and a *new* file grows `product_manifest` specs beyond the 4 product-spec files | registry evolution (e.g. new `billing.md` attached to a standard-tier rule) | **Required** parity pin: derived L3 set == the 4 current product-spec files, L0 == `[]` (D3). If product specs legitimately grow later, the pin is updated deliberately with the registry change. Alternative hardening (not now): derivation excludes rules carrying `page_types`; future option if drift recurs |
| F5 | **Billing leaks into prescriptions** | someone adds `signals` entries to the product registry → `scale_signal` M/L → effective tier standard/production | `prescription_lacks_rule: [billing]` eval case (double gate: matcher `lacks_rule` + prescription key) |
| F6 | **L0 restraint regression** | `productization_level` vocabulary change or routing reorder | unit pin `domain_for("写个内部小工具脚本，解析日志") == "generic"`, `domain_for("做一个开源上传产品，商业化") == "product"` |
| F7 | **Precedence surprise** | product-signal task classifying as backend/frontend_ui (e.g. "做一个 SaaS 计费前端页面") | By design (constraint §3); documented in SPEC_SYSTEM row. No auto-fix — matcher stays classifier-first |
| F8 | **`prescription` import weight** | `prescription()` is heavier than `match_rules` (runs `assess_dimensions` + product/workflow manifests) | Only the 1 new eval case uses prescription keys; negligible suite cost. Do not route `has_rule` through `prescription` (keep matcher-level keys cheap) |
| F9 | **Env-lock false failures during implementation** | running `pytest tests/` while campaigns hold the lock → 21 unrelated failures | Verification plan uses lock-free gates; full suite only when lock is free (or `--no-lock`); failures attributed by the exit-5 signature in `pbatch/lock.py` / `retro.py:41` |

---

## 5. Migration steps (ordered, each independently verifiable)

1. **Land the registry** — create `product-specs/rules.yaml` (R1 normative
   content). Verify: `python -m pbatch rules --check` still 2 domains OK
   (file inert until step 3); YAML parses (`python -c "import yaml; yaml.safe_load(open('product-specs/rules.yaml'))"`).
2. **Add eval plumbing** (safe to land first — new keys unused until step
   5): `eval.py` three-site change (§2.5). Verify: `python -m pbatch eval`
   still 41/41.
3. **rule_matcher wiring** — `DOMAIN_REGISTRIES` entry + reset tuple +
   `domain_for` lazy routing (§2.1). Verify: `rules "做一个开源上传产品，
   商业化" --json` → `domain: product`, rules include
   `commercial-readiness`, `billing` skipped; `rules "写个内部小工具脚本，
   解析日志" --json` → `domain: generic`; existing 41 eval cases still
   pass (backend/frontend routing untouched).
4. **product.py derivation** (§2.2) — atomic with step 3 (module-level
   import of `rule_matcher`). Verify: `pytest tests/test_product.py` 13/13;
   `python -c "from pbatch.product import _SPECS_BY_LEVEL; print(_SPECS_BY_LEVEL)"`
   matches the pre-change literal sets (set-equality).
5. **CLI/selfcheck + non-empty guard** (§2.3, §2.4). Verify:
   `rules --check` → three `registry: OK` lines, exit 0; temporarily move
   the registry file → exit 1 (F2); restore.
6. **Eval case + pins** (§2.6, D2/D3 tests). Add the case to
   `evals/assess.yaml`; add required unit pins:
   - `tests/test_product.py`: derived L3 == the 4 product-spec files, L0
     == `[]` (parity, D3); `domain_for` product/generic routing (F6).
   - `tests/test_rule_matcher.py` (or `test_product.py`): product registry
     contains no frontend ids (F1); `_check_registries_cli`-level empty-
     registry exit 1 (F2).
   Verify: `python -m pbatch eval` → 42/42; `--filter
   l3-prescription-no-billing-without-signal --json` → `{"total":1,
   "failed":0}`.
7. **Docs** (R6): SPEC_SYSTEM §2 registry row (including the precedence
   note), AGENTS.md component rows + "双域"→"三域" wording, `selfcheck.py:5`
   comment. Flag (do not fix here): the stale "387" test count in
   AGENTS.md / CHECKS_REGISTRY.md is actually 978 (D5).
8. **Full regression** (lock-free when campaigns drain): `python -m pytest
   tests/` (expect 978: 956 pass baseline + 21 env-lock failures only when
   the lock is held — re-run after campaigns finish), `python quality.py
   --strict .`, `pi-batch check`, `make ci` (self-test + test + org). No
   Go or packaging gates touched.

Rollback: each step is independently revertible (registry file deletion
reverts R1; single-line tuples revert R2/R4; derivation is one function
plus the literal restoration in `product.py`). Steps 2–4 are pure-additive
until step 5 flips the checker — the only behavioral switch is `rules
--check` tightening (intended, check-only).

---

## 6. Testable acceptance mapping

| Acceptance (requirements §4) | Live testable assertion | Verification command |
|---|---|---|
| A1 — registry exists, `rules --check` passes incl. product schema | exit 0 **and** log line `[product] registry: OK` **and** (D1) registry non-empty — missing file ⇒ exit 1 | `python -m pbatch rules --check; echo $?`; `python -m pbatch rules --check 2>&1 \| grep "\[product\] registry: OK"`; negative: `mv product-specs/rules.yaml /tmp/ && python -m pbatch rules --check; echo $?` → 1, restore |
| A2 — `_SPECS_BY_LEVEL` derived; `tests/test_product.py` green | 13/13 pass incl. new parity pin (L3 == 4 files, L0 == `[]`) | `python -m pytest tests/test_product.py -q` → `13 passed` (14 with parity pin) |
| A3 — `rules "做一个开源上传产品，商业化" --json` prescription includes `commercial-readiness` | `domain == "product"` and `product-specs/commercial-readiness.md` ∈ some `rules[].files`; `billing` ∈ skipped | `python -m pbatch rules "做一个开源上传产品，商业化" --json \| python -c "import json,sys; d=json.load(sys.stdin); assert d['domain']=='product'; assert any('product-specs/commercial-readiness.md' in r['files'] for r in d['rules']); assert 'billing' in [s['id'] for s in d['skipped']]"` |
| A4 — eval case pins L3 prescription without Billing; suite green | eval 42/42; new case asserts `product_level: L3_product_feature`, `has_rule: [product-thinking, commercial-readiness]`, `lacks_rule: [billing, visual-core]`, `prescription_lacks_rule: [billing]` | `python -m pbatch eval` → `all 42 passed`; `python -m pbatch eval --filter l3-prescription-no-billing-without-signal --json` → `{"total": 1, "failed": 0, "failures": []}` |
| Regression gates | eval 42, tests 978 (lock-free run), quality strict, `pi-batch check`, `make ci` | §5 step 8 commands |
| F1 pin (D2) | merge-inheritance: product manifest contains no `visual-core` | covered by eval `lacks_rule: [visual-core]`; unit: `python -c "from pbatch.rule_matcher import load_registry; assert 'visual-core' not in load_registry(domain='product')['rules']"` |
| F6 pin | L0 restraint | `python -m pbatch rules "写个内部小工具脚本，解析日志" --json` → `domain: generic`, `rules: []` |

Out of scope (unchanged from requirements §6): direction #2 (scenario
keyword tightening), direction #3 (execution-prompt spec paths), classifier
changes, `load_registry` fail-open semantics, packaging, `_PRODUCT_SIGNALS`
/ `_SCENARIOS` vocabularies.
