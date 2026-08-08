Design validated — the comparison logic catches all three path forms (relative, `./`-prefixed, absolute), dedupes the dual candidates to one canonical path per domain, and produces no false positives (legit specs, template expansion).

## Summary

**Evidence verification** — all 5 cited claims + 8 additional checks independently re-confirmed against HEAD (`backend-specs/rules.yaml:164-170` block, promoted archive, existence-only `_check_rule`, unconditional required-rule leak, `rules --check` exits 0 with the pollution live, `--registry` ignored on the check path, `assert:`-only eval dispatch, 40/0 eval baseline, zero dependents on the rule id). Three new findings sharpened the design: `tests/test_agent_engineering.py:158` calls `check_registry` with one arg (optional-param constraint), `make ci`'s `test_rules_check_passes` is a CI canary that enforces migration ordering, and `learn promote` writes `--files` verbatim (the re-pollution vector).

**Design** (written to `docs/auto/runs/remove-repo-internal-policy-rule-py-godfile-spli-5fe4767b/artifacts/design-86184943/design.md`):

- **API changes** — `check_registry(registry, registry_paths=None)` additive optional param; `_check_rule` gains a resolved-path set; `_check_registries_cli` passes `_default_registry_paths(domain)` (resolve-normalized, deduped); one eval case in `assert:` form; two unit tests. No `rule_matcher.py` behavior change; `learn.py` untouched (R1.2 is documentation).
- **Compatibility constraints** — default-`None` preserves all existing callers; additive violation messages; clean registries ship green (verified only `PY-GODFILE-SPLIT` is self-referential); **R1 must land with R2** (else `test_rules_check_passes` breaks `make ci`); eval count 40→41 stays within the `>= 20` assertion.
- **Failure modes** — 8 enumerated (re-promotion vector, silent regression, false positives, template edge, cross-domain escape, load degradation, inert eval format, ordering slip), each with detection path and mitigation.
- **Migration** — 7 ordered steps, each with its own verification command; rollback is a 4-file `git checkout`.
- **Acceptance mapping** — A1/A2 → eval case + grep; A3 → pytest unit test (clean side via `rules --check`/shipped-registries test); A4 → eval total 41/0 + full `make ci`.

The design's code sketches were executed in isolation to confirm behavior before landing.
