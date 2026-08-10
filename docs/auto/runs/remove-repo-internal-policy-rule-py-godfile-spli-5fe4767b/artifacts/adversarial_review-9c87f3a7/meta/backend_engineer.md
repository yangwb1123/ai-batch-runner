Implementation complete. Summary:

## What was done

**Direction**: Remove repo-internal policy rule `PY-GODFILE-SPLIT` from the backend registry + fail closed on self-referential rule files.

**Analysis** (per mandatory thinking order): This is tool-data + tool-code, not a business capability. Data ownership: shipped registries (`backend-specs/rules.yaml`) vs repo-policy archive (`docs/rules/promoted/`) — the rule's canonical record stays in the archive, the shipped registry stops carrying it. No abstraction introduced — the gate is a resolve-normalized path-set membership test composed into the existing `check_registry` flow; the only "pattern" is fail-closed enforcement, matching the decision table (identified defect class, not speculation).

**Changes** (5 files, exactly the design footprint):
1. `backend-specs/rules.yaml` — deleted the `PY-GODFILE-SPLIT` block (file now byte-identical to HEAD)
2. `pbatch/rule_cli.py` — `check_registry(registry, registry_paths=None)` additive optional param; `_check_rule` + new `_check_self_reference` reject any `files`/`files_template` entry resolving to the registry itself; `_check_registries_cli` passes resolve-normalized, deduped `_default_registry_paths(domain)` → `rules --check` / `pi-batch check` / `test_rules_check_passes` inherit fail-closed with zero wiring
3. `evals/rules.yaml` — new `assert:`-form case `backend-manifest-excludes-repo-internal-policy` (`lacks_rule: [PY-GODFILE-SPLIT]`)
4. `tests/test_rule_matcher.py` — 2 tests (violation side + shipped-registries-clean side)
5. `docs/SPEC_SYSTEM.md` — registry hygiene note (R1.2 guardrail)

**Verification results**:
- A1/A2: both acceptance inputs produce manifests with no `PY-GODFILE-SPLIT` ✅
- A3: `rules --check` exit 0 (clean); violation side catches all three path forms (relative/`./`/absolute) with the exact design message; default arg backward compatible ✅
- A4: eval **41/0** (was 40/0), new case actively runs and passes; `test_rule_matcher.py` 26 passed; `checks/` 37 passed; `quality.py --strict` OK; `make check/self-test/quality/org` all exit 0 ✅
- `pytest tests/` + `make ci` test target: 934 passed, **19 failed — all environmental lock contention** (`Another pi-batch instance is running (pid=1684832…)` — our own 7×24 campaign holds the repo lock; identical signature to the previous campaign's documented finding, none touch rule/registry code)

**Deviations** (evidence-backed, in the report): extracted `_check_self_reference` helper (inline loop exceeded the 15-complexity org gate); top-level import of `_default_registry_paths`; and a rollback nuance — `pbatch/rule_cli.py` is **untracked** (parallel cli.py-split workstream never committed it), so the design's `git checkout` rollback doesn't cover it (manual revert of 3 blocks instead).

**Not committed** per pipeline instructions; change report written to `artifacts/implement-f9bb818d/task-1-implementation.md` for the acceptance gate.
