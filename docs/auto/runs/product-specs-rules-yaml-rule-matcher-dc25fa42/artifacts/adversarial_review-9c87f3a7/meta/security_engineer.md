All key claims verified live. I have enough to deliver the review.

---

# Security Engineer Review — product-specs declarative registry (direction 82a451b3)

**Scope:** the product-domain rule registry (`product-specs/rules.yaml`), registry-driven spec derivation (`pbatch/product.py`), domain routing (`pbatch/rule_matcher.py`), the tri-domain `rules --check` gate (`pbatch/rule_cli.py`), and the eval pins. Reviewed against the **live implemented tree** (implement landed mid-session, per qa_lead). I re-ran the security-relevant claims myself this session; full-suite `make ci` remains blocked by campaign lock (F9) — stated, not papered over.

## 1. Assets, trust boundaries, attacker capabilities, entry points

**Assets in this surface:**
- `product-specs/rules.yaml` + `ui-specs/rules.yaml` + `backend-specs/rules.yaml` — **policy/control data**: decide which spec files reach agent prompts (the "billing restraint" and "L0 restraint" are authorization gates on *spec→prompt* injection).
- `_SPECS_BY_LEVEL` (import-time derivation) — consumed by `assessor`, `profile`, `eval`.
- The rule **manifest** — explicitly "Prompts inject ONLY the manifest; agents load the files on demand" (`rule_matcher.py` docstring) → manifest content is agent instruction.
- Eval suite — regression pins for the control behavior.
- Agent context + task artifacts — exfiltration sinks.

**Trust boundaries** (this direction's relevant ones):
1. **Tool-root registry (trusted, repo-maintained)** vs **CWD registry (project-controlled)**. `_default_registry_paths` merges *both*: `[tool_root/rel, CWD/rel]`, per-key CWD wins, **silently** (no log of which candidate was used). The portability contract ("copy pi-batch into another project") means the CWD side is routinely attacker-influenced when the tool is run against a cloned/untrusted repo — yet AGENTS.md §5 lists only "agent 输出、编排者 JSON" as untrusted inputs, and defends *role_dir* against path traversal. The registry path surface lacks the same containment.
2. **Task text (untrusted)** — drives domain routing via keyword scoring (`domain_for`, `productization_level`), spoofable by design (F7).
3. **Agent output (untrusted)** — pre-existing failure-signature handling; unchanged by this direction.
4. LLM provider = out-of-bounds sink (files read into model context are sent to the provider).

**Attacker capabilities** (realistic for this tool): file-write inside the project directory (malicious repo clone, compromised project, co-tenant CI workspace), and control of task text. No network/remote surface exists on this tool; no authN/session/token material in this surface (mapped in §3).

**Entry points:** `pi-batch rules [--check|--json|--llm-prompt]`, `pi-batch assess`, `pi-batch eval`, `pi-batch profile`, `pi-batch check` (registry step), and **import-time** `_derive_specs_by_level()` — any command importing `pbatch.product`/`pbatch.assessor` parses the merged product registry before any argument is validated.

## 2. Findings (severity-sorted)

### F1 — Medium (required to close): registry `files`/`files_template` accept absolute/escaping paths; CWD-merged registry injects them into agent prompts → out-of-repo file disclosure
- **Evidence (Verified, live):** a temp-dir `product-specs/rules.yaml` with `files: [/home/user/.ssh/id_rsa, /etc/passwd, /proc/self/environ]`, `min_tier: demo, required: true`:
  - `pi-batch rules "做一个开源上传产品，商业化" --json` → the rule appears in the manifest, `required: True`, absolute paths intact (`rule_matcher.py:_resolve_files` passes paths through unmodified; `load_registry` merges CWD over tool-root per key).
  - `pi-batch assess "做一个开源上传产品，商业化"` → prescription prints `- [必选] evil-read (demo) … /home/user/.ssh/id_rsa /etc/passwd`; the `规范:` line of the product block contains both absolute paths.
  - Import-time: `_SPECS_BY_LEVEL['L3_product_feature']` contains the absolute paths — every assess/eval/profile process in that directory inherits them.
  - `check_registry()` on the same rule → **NONE (passes)**: only existence is validated, `/etc/passwd` and `/proc/self/environ` exist. No containment to repo root, no `../`-escape check, no restriction on the `files_template` base either (verified: a `../../../../home/u1/{profile}.md` template passes).
- **Preconditions/steps:** attacker controls project content (or a repo the tool is copied into); victim runs any pi-batch command there. Routing to the product domain needs only task-text words ("产品/商业化"); backend/frontend shadow registries work identically.
- **Impact:** agent prompt instructs the model to read files **outside the repo** — `~/.ssh/id_rsa`, `/etc/passwd`, `/proc/self/environ` (agent process env often holds provider API keys). File contents enter model context (sent to the LLM provider) and may be written into task artifacts/decision logs. This crosses the tool's own read boundary (evidence reads are otherwise repo-scoped, bounded, and secret-masked in memory handling).
- **Remediation:** (a) resolve `files`/`files_template` and require containment within the repo root (the exact pattern already used for `role_dir` lookup, AGENTS.md §5); (b) `load_registry` logs which candidate was merged (source attribution for CWD shadowing); (c) `check_registry` gains a containment violation (fail closed); (d) `_derive_specs_by_level` reuses the same resolution. All shipped registries use repo-relative paths, so containment breaks nothing.
- **Regression test:** unit — registry rule with `files: ["/etc/passwd"]` → `check_registry` returns a containment violation; `match_rules`/`_derive_specs_by_level` never emit a path that resolves outside repo root; plus a `files_template` escape case.

### F2 — Medium: R4′ guard is frontend-masked and load/check are CWD-asymmetric
- **Evidence (Verified, live):**
  - *Frontend masking* (qa_lead Finding 2, re-verified by simulation): with the tool-root `ui-specs/rules.yaml` deleted, `load_registry(domain="frontend_ui")` returns the built-in `_DEFAULT_REGISTRY` rules → non-empty → `rules --check` exits 0. The new guard's promise ("empty registry never reports OK") does not hold for frontend — the domain whose built-in defaults are non-empty. Backend/product correctly → empty → exit 1 (verified: empty CWD product registry → `rules --check` exit 1).
  - *Load/check asymmetry*: `load_registry` merges tool-root + CWD candidates, but `check_registry`'s file-existence check is CWD-relative. Running `rules --check` from any directory other than the tool root spuriously fails the frontend registry on `ui-specs/engineering/*.md` (live: temp-CWD run reported "missing file ui-specs/engineering/form-table-state.md" — the files exist in the tool root). Conversely, a shadowed registry is validated against the wrong base.
- **Impact:** the gate this direction added to close the vacuous-A1 gap gives false OK (frontend deletion/typo) and false FAIL (non-root CWD); control-integrity weakness in the checker itself.
- **Remediation:** per-domain `any(p.exists() for p in _default_registry_paths(domain))` → else exit 1 (qa_lead's exact spec, which covers frontend); resolve existence checks against the same candidate list the loader uses; emit a warning when a domain falls back to built-in defaults or a CWD shadow was merged.
- **Regression test:** monkeypatch `_default_registry_paths("frontend_ui")` to nonexistent paths → `SystemExit(1)`; run `check_registry` with files existing only at the tool-root candidate → no violation.

### F3 — Medium: the F5 prescription pin is vacuous against its own trigger (restraint-gate regression undetected)
- **Evidence (code-path Verified; live simulation per qa_lead):** the eval case `l3-prescription-no-billing-without-signal` pins `prescription_lacks_rule: [billing]` only. `prescription()` (assessor.py:150) selects by `TIER_ORDER[item.tier] <= rank` where rank comes from `scale_signal()` (assessor.py:91) — the registry's `signals`/`signal_weights`. A future signals escalation (F5's exact trigger) raises the effective tier to standard and `commercial-readiness` (min_tier standard, `required_when_risk` — and the product registry's `risk.high` list already marks 商业化/订阅/计费/付费) enters the prescription while `billing` stays absent (it is page_types-gated at the matcher). The A4 restraint guarantee and the design's F5 detection row are therefore unpinned by the case as written.
- **Impact:** silent degradation of the "high-cost implementation never enters the prescription without explicit commercial mechanics" control with no red test; the eval suite — this direction's own CI tripwire — cannot see the failure.
- **Remediation (one-line):** extend the case: `prescription_lacks_rule: [billing, commercial-readiness]` (plus `open-source-readiness` for the L-escalation case), and add `prescription_has_rule: [product-thinking, completion-evidence]` to pin the prescription staying non-empty. Acceptance: `python -m pbatch eval --filter l3-prescription-no-billing-without-signal --json` must **fail** after the escalation is applied (currently passes — the defect).
- **Regression test:** the extended case itself (fail-closed by construction).

### F4 — Low: silent parse-failure fail-open for frontend; no source attribution
- **Evidence (Verified, code):** `load_registry` wraps each candidate in `try/except Exception: continue` with **no log** — a YAML-broken or deep-nested (RecursionError) registry is indistinguishable from a missing one. For product/backend that degrades to `[]` (fail-closed, warning only in `_derive_specs_by_level`); for frontend it silently falls back to the non-empty built-in defaults → the L0/L1-style restraint for the frontend registry degrades silently (over-specification), and `rules --check` still exits 0.
- **Remediation:** log a warning naming the skipped candidate; optional — count parse failures in `rules --check` (frontend included) as violations.

### F5 — Low: unbounded rule counts from a shadowed registry → manifest/prompt amplification
- **Evidence (Verified):** PyYAML `safe_load` memoizes aliases (12-level alias chain: 594B input, 0.002s, shared objects) — **no billion-laughs memory bomb**; 2MiB input bound + double-check + UTF-8 validation hold. Residual: an attacker registry can declare an unbounded number of rules → `match_rules` output, `--json`, and `--llm-prompt` (which lists *all* rule ids) grow without a cap; deep nesting → RecursionError caught (see F4). Local-only impact, same precondition as F1.
- **Remediation:** cap rules per manifest in `match_rules` (e.g., the `format_manifest` limit=8 pattern extended to the raw output) with a truncation marker.

### F6 — Low: keyword routing on untrusted task text is spoofable (documented F7, residual)
- **Evidence (Verified, code):** `productization_level("…product.py…")` matches the L3 term "product" by substring; "配额/quota" in `use_case_types.billing` fires on internal quota features (product_thinker Q5). Consequences are bounded: matcher-level loading is *documentation*, and the prescription-level double gate (empty `signals` → effective tier demo) contains the assess path (verified live: A3 billing skip, A4 restraint). Residual risk is over-prescription/prompt bloat, not data. Tracked by design as direction #2 deferral.
- **Remediation (optional):** negative eval case "配额/额度 without 订阅/计费/付费" → billing skipped; keep the double gate.

### F7 — Info: eval.py case-id uniqueness (qa_lead Finding 3)
- **Evidence (Verified, code):** `load_eval_files` enforces non-empty ids only; duplicates would both run under `--filter`. Add a `seen` set; no security impact today (42/42 unique).

### F8 — Info: import-time derivation is per-process
- `_SPECS_BY_LEVEL` is computed once per process at `import pbatch.product`; a registry swapped mid-process is invisible to that process (single import — benign), but the shadow merge is silent (see F1b/F4). TOCTOU between stat and read is already handled by `read_text_bounded`'s double check.

## 3. Abuse-case table

| Category (mapped) | Abuse case | Reachable? | Vector | Severity |
|---|---|---|---|---|
| Identity spoofing (analog: routing spoof) | Task text padded with "商业化/订阅" forces product domain + billing page-type; "重构 product.py" hits L3 via substring | Yes (F6, F7) | Untrusted text → keyword routing (deterministic, documented) | Low (contained by double gate + eval pins) |
| Replay | Session/token replay | N/A — no tokens/sessions/network in this surface; gate-PASS reuse is guarded elsewhere (fingerprint), unchanged by this direction | — | N/A |
| Cross-tenant (analog: cross-repo contamination) | Malicious cloned repo's `product-specs/rules.yaml` merges over the shipped registry and injects `~/.ssh/id_rsa` + `/etc/passwd` as REQUIRED spec files into `assess`/`rules` output → agent reads them → provider context + artifacts | **Yes (F1, live-verified end-to-end)** | CWD registry merge, no containment, no source attribution; `rules --check` existence-only validation passes | **Medium** |
| Proxy/header forgery, SSRF | N/A — no proxy/HTTP entry points in this surface; agent network calls are pre-existing and out of scope | — | — | N/A |
| Resource exhaustion | Alias bomb | No — `safe_load` shares alias objects (verified: 12 levels/594B → 0.002s) | — | N/A |
| Resource exhaustion | Deep-nest RecursionError → silently skipped registry (frontend falls back to defaults) | Yes (F4) | 50k-deep YAML, ~0.7s, caught by bare except | Low |
| Resource exhaustion | Unbounded rule count → unbounded `--json`/`--llm-prompt`/manifest | Yes (F5) | Attacker registry with N rules | Low |
| Sensitive-data leakage | Registry-driven path disclosure: `/proc/self/environ` (agent env: provider API keys), `/etc/passwd`, `~/.ssh/id_rsa` read into model context | **Yes (F1)** | Prompt-injected absolute paths; import-time `_SPECS_BY_LEVEL` contamination | **Medium** |
| Control-integrity (authorization analog) | Frontend registry deleted/broken → defaults used, `rules --check` exits 0 (vacuous OK persists for frontend) | Yes (F2) | `_DEFAULT_REGISTRY` non-empty; existence check CWD-relative | Medium |
| Control-integrity | Signals escalation leaks `commercial-readiness` into prescription with no red test | Yes (F3) | `prescription()` tier selection; eval pin covers billing only | Medium |

## 4. Positive controls verified

| Control | Evidence (this session / cited) |
|---|---|
| `yaml.safe_load` at every untrusted YAML boundary (config, registries, evals) — no code exec | Verified, code (`config.py:41`, `rule_matcher.py`, `eval.py`) |
| Bounded reads: 2MiB cap, stat+read double check, UTF-8 validation, fail-closed ValueError | Verified, code (`text_io.py`) |
| Domain reset: backend/product never inherit frontend defaults; F1 merge-inheritance pinned (`test_product_registry_has_no_frontend_defaults`, eval `lacks_rule: [visual-core]`) | Verified, code + tests |
| R4′ non-empty guard: missing/empty product/backend registry → `rules --check` exit 1 (closes vacuous A1) | **Verified live** (empty CWD product registry → exit 1) |
| Self-referential rule check (registry → own file) fail-closed, resolve-normalized | Verified, code (`_check_self_reference`) |
| L0 restraint: L0 → generic → zero rules | Verified live (`domain: generic, rules: []`); pinned (`test_domain_for_routes_product_and_generic`) |
| Billing double gate: matcher `page_types: [billing]` + `min_tier: production`; prescription-level empty `signals` | Verified live (A3 skip; explicit-mechanics positive case loads; qa_lead's A4 sim) |
| Vacuous eval case → exit 2 (fail closed); invalid `--llm-json` → exit 2 | Verified, code |
| Registry schema gate wired into `pi-batch check` (selfcheck.py registry step) | Verified, code |
| Import-cycle constraint honored (lazy `domain_for` import; module-level import would cycle) | Verified, code + import smoke (qa_lead) |
| A1 → A4 acceptance: `rules --check` 3× OK from repo root; eval 42/42; test_product 15/15; test_rule_matcher 28/28 | Cited from qa_lead/backend_engineer (I re-verified the guard, masking, and injection claims live; did not run full suite — campaign lock, F9) |

## 5. Residual risks and prioritized validation plan

**Residual risks:** (1) registry path disclosure (F1) — accepted only if "untrusted project content" is explicitly out of threat model; it is *not* consistent with the repo's own §5 boundary (role_dir traversal is defended, registry files are not); (2) frontend vacuous-OK and load/check CWD asymmetry (F2); (3) unpinned F5 restraint regression (F3); (4) silent parse-skip fail-open for frontend (F4); (5) documented keyword-spoof surface (F6, by design).

**Prioritized validation plan (in order):**
1. **F1 (required):** containment for `files`/`files_template` in `_resolve_files` + `check_registry`; source-attribution log in `load_registry`; unit tests for absolute-path and `../`-escape rejection; re-run `pi-batch eval` + `rules --check`.
2. **F3 (required):** extend `l3-prescription-no-billing-without-signal` per §2-F3; verify the case goes red under the escalation simulation, green without.
3. **F2 (recommended):** per-domain file-presence guard incl. frontend; existence checks against loader candidates; tests.
4. **F4/F5 (optional):** parse-skip warnings; rule-count cap.
5. After campaign lock drains: full `pytest tests/` (982) + `make ci` — expected 960 passed / 21 exit-5 lock failures / 1 skipped, attributable only by the F9 signature; then re-run `python quality.py --strict .` (green this revision).

**Verdict:** no Critical/High finding on the implemented change itself; the direction's core claims (F1 merge-inheritance hazard closed, vacuous-A1 closed for backend/product, D3 parity pin, L0 restraint, billing double gate) verify live. Two **required** corrections before closure — F1 (registry path containment, closing the disclosure vector this direction extended to a third domain + import-time) and F3 (the F5 prescription pin is vacuous against its own trigger) — plus the F2 checker hardening (recommended). All are one-liners with exact tests specified above.
