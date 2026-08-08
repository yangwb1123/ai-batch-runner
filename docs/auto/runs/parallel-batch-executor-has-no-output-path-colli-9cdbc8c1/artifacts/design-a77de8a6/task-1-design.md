Design complete. Summary of the work:

## Evidence verification — all claims confirmed live

Every claim in the requirements evidence was re-checked against the tree: `runner.py:935/956/886` (plain `ThreadPoolExecutor` + submission-order submit + retry loop, no collision guard), `cli_batch.py:496`, `waves.py:49/56` (`_conflicts`/`plan_waves`), **zero production importers of waves** (only 5 test files), `meta.py:392` dedupe, `test_pi_batch.py:425` (no collision coverage), TaskNode test-only construction, runner.py **996/1000 lines** with only `pi-batch.py` legacy-exempt, Python ≥3.9, exit-2 house pattern, 3 `run_parallel` callers. All ✅ exact.

## Design — `docs/auto/design/pbatch-ba9cd1a4-design.md` (544 lines, repo convention)

- **API changes**: new leaf module `pbatch/parallel_plan.py` (`OutputCollisionError`, `collides`, `detect_collisions`, `resolve_plan`, `tasks_to_nodes`, `plan_waves_for` — full reference implementation included); `run_parallel(tasks, ..., waves=None)` unified into **one wave loop** (plain path = single wave → 999 lines, passes the 1000-line `make ci` gate); `cli_batch._execute_batch` catches → stderr report → exit 2; AGENTS.md bullet.
- **Compatibility**: additive `waves=None` default, 3 callers untouched, same-cwd batches unaffected, serial mode / `waves.py` / `fabric_sched.py` / `pbatch/__init__.py` untouched, `waves` plans relative to the post-`--reuse` task list.
- **Failure modes** (F1–F10): collision → exit 2 no-agent; invalid plan → fail closed; budget cap mid-wave → `SystemExit(3)` with no next wave; pipeline/campaign propagate loudly; reuse-masking, symlink aliasing, dry-run gap, case-insensitive FS, O(n²) documented.
- **Migration**: 4 phases (new module → runner rework → tests → docs) + rollback; `git diff --stat` budget (+2 runner, +5 cli_batch).
- **Acceptance mapping**: every R1–R4 sub-requirement mapped to concrete assertions in T1–T4 (with the corrected containment fixture, same-cwd regression sub-case, and CLI subprocess companion asserting returncode 2 + stderr).

## Three corrections the spec missed (flagged in §0.2)

1. **C1 — the literal R1.2b rule is self-contradictory**: "output inside the other task's cwd" flags every pair of tasks sharing a cwd — which is exactly how the runner builds its own batches (campaign analysis `cwd=root`, `from_dir` stages, default-cwd tasks.yaml), and **the spec's own acceptance T3 requires a same-cwd batch to be schedulable**. Fixed: containment checked only between *distinct* cwds (grounded in the verified fact that outputs are runner-written atomically via mkstemp+rename).
2. **C2 — `plan_waves` (write-set intersection) cannot express path containment**, so raw `waves.plan_waves` output can fail `run_parallel`'s own validation; `plan_waves_for` tightens with the blocked-defer doctrine.
3. **C3 — the spec's "thin addition" arithmetic misses the line budget** (naive branch = +7 → 1003, and `print_summary` is public API, not movable); the unified wave loop is required, landing at 999.
4. **C4 — R2.5 "empty plan → no-op" contradicts R2.3's fail-closed coverage**; design chooses fail-closed (silent no-op would report success for a batch that ran nothing).
