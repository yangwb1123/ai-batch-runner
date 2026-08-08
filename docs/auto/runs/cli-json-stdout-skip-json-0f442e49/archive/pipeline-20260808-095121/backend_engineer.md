All checks complete. Final verification summary of the deliverable state:

- **4 affected test files: 100/100 PASS** (incl. all 8 new acceptance tests)
- **Live pollution reproductions fixed**: backend `--dir . --json` and frontend `--dir examples --json` now emit exactly one JSON document; summaries on stderr
- **Non-json mode byte-identical** to HEAD (diff of stdout+stderr: empty)
- **`--skip` semantics live-verified**: 2→1 (scratch) →2 (idempotent) →2 (empty no-op)
- **health**: `healthy: True`, plain `json.loads`, `raw_decode` gone
- **Gates**: `quality.py .` OK, `cli.py check-test` OK, eval 42/42, org/capabilities/graph valid
- **Excluded failures (both pre-existing/environmental, verified)**: `make ci check` line-length warnings on regex lines present identically at HEAD (10/2 counts unchanged); 21 full-suite failures all "Another pi-batch instance is running (pid=1547473)" — the campaign runner holding the lock, unrelated to this change

---

# Required Output

## 1. Analysis

**Business boundary / module ownership**: This capability (checker CLI contract) belongs to `scripts/` (deterministic gates) + `pbatch/health.py` (the consumer). No new module — the design is a contract repair of existing boundaries.

**Data ownership**: stdout is the machine contract (exactly one JSON doc under `--json`); stderr is the human/retry-feedback channel. `DEFAULT_EXCLUDES` is owned by each checker as module state — the fix builds the union as a **new set** (`DEFAULT_EXCLUDES | {...}`), never mutating the constant (F8: no cross-run leakage).

**Change points**: Nothing varies — this is a fixed CLI contract, not a strategy. **No pattern — composition suffices**: no interface, no factory, no Base* class. The one abstraction reused is `scan_directory`'s existing segment-exact predicate (`any(part in excludes for part in parts[:-1])`), inherited from the checker family (design-intelligence `_collect_files`). Introducing any abstraction here would violate design-patterns.md red lines (one-implementation, speculative).

**Dependency direction**: Transport (CLI) → application logic — flat within each checker; `health.py` (application consumer) depends on the checkers' public CLI contract only (subprocess + stdout), never their internals. `raw_decode` removal eliminates the adapter that tolerated contract pollution — the contract is now fail-closed (`-1` → `healthy: False`).

**State/consistency**: No entity state, no transactions, no persistence — the persistence gate (persistence-modeling.md §12) doesn't apply (no tables/ORM entities). The relevant "strong consistency" rule is the stdout purity invariant under `--json`, enforced by routing ALL human summaries to stderr.

## 2. Implementation

**API-1** `--skip NAMES` (both checkers): `parse_argv` gains the flag (backend 7-tuple, frontend 6-tuple); scan set = `DEFAULT_EXCLUDES | {s for s in skip.split(",") if s}` — union, never replace; `--skip ""` → no-op; explicit file targets unaffected; trailing `--skip` without value stays consistent with the file's `--dir` convention (visible "missing target", F3).

**API-2** stdout purity: `print(..., file=sys.stderr if want_json else None)` on the two backend fall-through lines (`_finish`, which gained `want_json=False`) and the frontend OK line — conditioned on `want_json` only, so `-o`-only and human modes are byte-identical by construction (C1/C2 guards pin this).

**API-3** `pbatch/health.py`: deleted the `check-backend-quality` special case and the `index("{")` + `raw_decode` tolerance; all three checkers get `--skip SKIP` uniformly; plain `json.loads` with `except ValueError` → `-1` sentinel (F4/F5: any future stdout regression becomes a visible `healthy: False` alarm, never a silent misread).

**Tests** (8 new, all green): AC1 `test_backend_json_stdout_contract` (ok/skipped/violation paths, each `json.loads`-parseable, summary in stderr); AC2 `test_backend_skip_flag` (2→1→2→2 incl. idempotence + empty no-op); AC3 `test_frontend_skip_flag` (with `--skip e2e` negative control — replace-implementation would yield 3); AC4 `test_health_checker_plain_json_contract` + `test_health_main_json_shape` (direct-subprocess equivalence, key set, `raw_decode`/`JSONDecoder` source guards); AC5 `test_json_clean_path_stdout_pure` (ui-spec family reference); R2 `test_frontend_json_ok_path_pure`; C1 `test_backend_non_json_unchanged`; C2 `test_backend_o_flag_summary_on_stdout`.

**One documented deviation from the design's test sketch**: AC1(c) fixture `// TODO` cannot yield exit 1 (TODO is a >10-marker strict-only warning) — implemented with `console.log('x')`, a genuine always-fail error, preserving the AC's intent (violation path purity).

## 3. Self-check (architecture.md §8 / evolution.md §6) + verification

**architecture.md §8 (state mgmt)**: N/A — no entity state in scope; the checker contract invariant is centralized (single `_finish`/`main` summary point per checker), not scattered.
**evolution.md §6 (15-point review)**: boundaries clear (scripts = gate producers, health = consumer via public CLI); no cycles; no framework coupling; no god classes added; DTO/domain mixing N/A; no new interfaces (no single-impl abstraction); no switch-based strategy; state transitions N/A; no cross-module internals access; SDK isolation N/A; transaction boundaries N/A; both modules independently testable (subprocess-level tests); implementation change required **zero** caller changes (additive flag, no existing invocation passes `--skip`); no pattern introduced → §6.15 satisfied.
**Production readiness**: contracts explicit (stdout = one JSON doc), error model fail-closed (`-1`/exit codes unchanged), idempotent (`--skip` union is deterministic, re-runnable), observability (summaries on stderr feed retry prompts), verification honestly reported below.

**Commands actually run**:
```
python -m pytest tests/test_backend_specs.py tests/test_frontend_quality.py \
  tests/test_tools_health.py tests/test_ui_check.py      → 100 passed
python -m pytest tests/                                   → 1024 passed, 21 failed
  (all 21 = lock exit-5 "Another pi-batch instance is running (pid=1547473)" — the
   campaign runner holds the pi-batch lock; environmental, not this change)
python scripts/check-backend-quality.py --dir . --json    → json.loads OK (was JSONDecodeError)
python scripts/check-frontend-quality.py --dir examples --json → json.loads OK (was JSONDecodeError)
git diff HEAD-version vs current in non-json mode         → byte-identical
python pi-batch.py health --json                          → healthy: True, keys unchanged
python pi-batch.py eval                                   → all 42 passed
python quality.py .                                       → OK
python cli.py check-test                                  → OK
python pi-batch.py org check / capabilities check / graph extract → valid, no cycles
make ci                                                   → `check` step FAILS on 10+2 pre-existing
  >100-char regex lines (identical count at HEAD — baseline, not introduced)
```

**Could not run**: a clean full `make ci` — blocked by (a) the pre-existing baseline style warnings on regex lines (verified identical at HEAD, out of scope), and (b) the campaign lock held by PID 1547473 (my own orchestrator) causing 21 subprocess-spawning tests to exit 5. Both are environmental; the change-specific gates and all 8 new acceptance tests are green.

**Note on authorship**: the target files were concurrently modified by a sibling agent in this meta stage while I worked; I verified the merged state against the design (§1–§7), confirmed the docstring R5 updates, and validated every acceptance criterion with live reproductions and the gate suite above.
