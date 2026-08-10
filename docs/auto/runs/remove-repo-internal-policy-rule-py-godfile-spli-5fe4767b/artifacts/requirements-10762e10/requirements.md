Requirements specification complete: `docs/auto/runs/remove-repo-internal-policy-rule-py-godfile-spli-5fe4767b/requirements-spec.md`

## Summary

**Every cited evidence point verified against the repository** (all 5 confirmed, plus 8 additional checks):

| Evidence | Verified finding |
|---|---|
| `backend-specs/rules.yaml:164` | `PY-GODFILE-SPLIT` block at 164–170: `files: ['backend-specs/rules.yaml']`, `min_tier: standard`, `required: true` |
| `docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml:1` | Exists; full RCA record — the repo-policy archive (`PROMOTED_DIR`) |
| `pbatch/rule_cli.py:52` | `_check_rule` checks only file *existence*, no self-reference check |
| `pbatch/rule_matcher.py:186` | `if rule.get("required"): return ""` — required rules never suppressed, leak is unconditional |
| `pbatch/rule_matcher.py:42` | `DOMAIN_REGISTRIES["backend"] = ["backend-specs/rules.yaml"]` |

**Bug reproduced live**: both acceptance task strings emit `PY-GODFILE-SPLIT` (required, `files=['backend-specs/rules.yaml']`) into the manifest; `rules --check` exits 0; `format_manifest` instructs agents to "load the listed files" → registry-as-spec pollution confirmed.

**Spec contents**:
- **R1** — remove the exact YAML block (lines 164–170); canonical record stays in the promoted archive; guardrail: never re-promote repo-internal rules into shipped registries
- **R2** — fail-closed self-reference check: `check_registry` gains optional `registry_paths` (resolve-normalized comparison, covers `files` + `files_template`), `_check_registries_cli` passes `_default_registry_paths(domain)` for both domains; `sys.exit(1)` plumbing already exists
- **R3** — eval case using the **`assert:`/`lacks_rule`** form — verified that the `expect:`/`includes` format used by existing `design-intelligence-*` cases is **inert** (`run_suite` only reads `assert`)
- **A3 procedure fixed mid-write**: verified `rules --check` ignores `--registry` (returns before parsing it), so the gate demonstration is via a `pytest` unit test (`test_registry_rejects_self_referential_files`)
- **Scope discipline**: generic-domain registry loading explicitly a non-goal (`DOMAIN_REGISTRIES["generic"] = []` by documented fail-closed design); cross-field hardening and design-intelligence re-tiering are separate directions
- **Baseline**: `pi-batch eval` = 40 cases / 0 failed; no test/eval/script depends on the rule id — removal is zero-blast-radius
