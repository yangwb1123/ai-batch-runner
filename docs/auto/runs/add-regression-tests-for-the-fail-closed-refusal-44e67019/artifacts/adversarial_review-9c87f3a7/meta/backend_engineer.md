All complete. Here is the required output.

---

## 1. Analysis

**Module boundary**: this is tooling, not a business service — the "capability" is a deterministic gate (fail-closed refusal detection) owned by `scripts/`, consumed by the validator registry (`pi-batch.yaml`) and surfaced via the selfcheck module (`pbatch/selfcheck.py`). No service/table/HTTP layer involved.

**Data ownership**: no data entities. The contract is the gate's observable behavior (exit 0/1/2, 10 markers, `REFUSAL-CHECK` diagnostics) + the config registry entry (`refusalcheck`) + the canonical script path. The example copy under `examples/snaplink-platform/scripts/` was a byte-identical orphan (E2/F5) — deleted per the design's deletion branch (D3).

**Change points**: markers/exit codes (frozen by NG1 — tests pin them), registry membership (additive, NG2), selfcheck step set (additive). Nothing varies in the product sense — **no pattern; composition suffices**. `_run_refusal_step` is a plain function with injectable parameters (`validators`/`script`) for unit testing — the only abstraction needed is test seam, not a pattern. Per design-patterns.md §3 red lines: no interface, no factory, no base class, no speculative extension points.

**Dependency direction**: `check_main` → `_default_steps` → `_run_refusal_step` → `pbatch.config.VALIDATORS` (import-time registry load, F2) → subprocess gate. No cycles; domain (script) never imports tooling.

**Mandatory thinking order applied**:
1. **Business boundary**: `scripts/` owns the gate; `pbatch/selfcheck.py` owns the "is the tool healthy" capability.
2. **Data ownership**: the registry entry in `pi-batch.yaml` is the single source of truth; tests read it via `pbatch.config.VALIDATORS`, never duplicate it.
3. **Strong consistency**: N/A (no transactions); the "atomic" rule here is fail-closed — any missing piece returns False, never a no-op print (A4.3).
4. **Change points**: markers (auto-derived via `runpy.run_path`, D4), registry count (A5.1 test keeps AGENTS.md truthful permanently).
5. **Dependency direction**: one-way, verified.
6. **Composition**: suffices — no pattern introduced.
7. **Persistence**: **N/A — no tables/ORM entities created; persistence-modeling.md §12 gate does not apply** (no identity/keys/money/NULL decisions exist in this change).
8. **Production readiness**: contract frozen (NG1), error model = exit codes + diagnostics pinned by A1.x tests, capacity = 3 × ~10 ms smoke runs, zero CI wall-time (`make ci` doesn't run `pi-batch check`), observability = `CHECK [refusal] …` lines, verification honestly reported below.

## 2. Implementation

| File | Change |
|---|---|
| `tests/test_no_refusal.py` | **New**, 27 cases (10 parametrized markers): A1.1–A1.7 gate contract, A2.1–A2.3 canonical-copy, A3.1–A3.2 registry, A4.1–A4.3 selfcheck step + unknown-`--steps` rejection, A5.1 AGENTS.md count guard |
| `pi-batch.yaml` | `refusalcheck: "python {cwd}/scripts/check-no-refusal.py {output}"` added after `completion` (23 → **24** entries) |
| `pbatch/selfcheck.py` | `_run_refusal_step(validators=None, script=None) -> bool` (in-process, fail-closed, 3 smoke runs); registered as `("refusal", …)` in `_default_steps` before eval; `check_main` now rejects unknown `--steps` names with **exit 2** (FIND-2/D2) and dispatches callable steps |
| `examples/snaplink-platform/scripts/check-no-refusal.py` | **`git rm`** (staged) — duplicate eliminated |
| `examples/snaplink-platform/requirement-pipeline.yaml:44` | `python` → `python3` (spec §6.2 optional normalization; regex accepts both) |
| `AGENTS.md:70` | "6 个验证器" → "**24 个验证器（含 `pyquality`、`refusalcheck`）**" |
| `scripts/check-no-refusal.py` | **zero edits** (NG1) — byte-identical to pre-change state |

## 3. Self-check (architecture.md §8 + evolution.md §6 + verification actually run)

| Check | Result |
|---|---|
| `python -m pytest tests/test_no_refusal.py -v` | ✅ 27 passed (0.67 s) |
| `python -m pytest tests/test_org.py -k check tests/test_agent_engineering.py::test_check_subcommand_passes` | ✅ 13 passed (pinning tests green: `_run_step` signature, `--steps org,capabilities,tools` → "all 3 gates passed") |
| `python pi-batch.py check --steps refusal --skip-eval` | ✅ exit 0, `CHECK [refusal]` × 6 lines |
| `python pi-batch.py check --steps not_a_real_step --skip-eval` | ✅ exit 2, "unknown step(s)" |
| `python pi-batch.py check --skip-eval` (default set) | ✅ "CHECK: all 8 gates passed" |
| `python pi-batch.py check --steps eval --skip-eval` | ✅ "all 0 gates passed", exit 0 (semantics preserved) |
| `grep -rn "check-no-refusal" examples/` | ✅ exactly the 8 `validate_cmd` lines, all `{cwd}/scripts/…` form |
| `yaml.safe_load` validator count | ✅ 24 |
| `python quality.py --strict .` | ✅ OK (function ≤50 lines, complexity ≤15 — `_run_refusal_step` ~46 lines) |
| `make quality` / `make org` / `make self-test` | ✅ pass |
| `git diff scripts/check-no-refusal.py` | ✅ empty (DoD: gate byte-identical) |
| `make test` | ⚠️ 987 passed / **21 failed — all pre-existing**: verified via `git stash` that `test_pi_batch.py` + `test_local_fixes.py` failures reproduce identically without my changes (unrelated concurrent-campaign work in this tree) |
| `make ci` (check gate) | ⚠️ fails on `pbatch/cli_batch.py` (501 lines > 500) — **untracked file from a prior campaign run, not touched by this change**; fails identically on the stashed baseline |

**Could not run / pre-existing**: `make ci` full pass is blocked by the pre-existing `check` gate failure (`pbatch/cli_batch.py` filesize — a god-file from earlier dogfooding work awaiting its own refactor); 21 unrelated test failures in `test_pi_batch.py`/`test_local_fixes.py` exist on the baseline. All acceptance criteria of this task (A1.x–A5.1, FR-1…FR-4, DOC-1) are green and none of those failures intersect this change's blast radius (design FM1–FM13 register — none triggered).
