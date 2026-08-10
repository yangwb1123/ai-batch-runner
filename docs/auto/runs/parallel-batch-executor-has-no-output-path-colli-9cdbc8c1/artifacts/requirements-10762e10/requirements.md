The specification is complete and internally consistent. Summary of what was produced:

**Deliverable:** `docs/auto/requirements/pbatch-ba9cd1a4-requirements.md` (repo's convention for campaign spec outputs).

**Evidence verification — all 8 citations confirmed live:**
- `runner.py:935/956/886` — `ThreadPoolExecutor` + submission-order `pool.submit` + retry loop, no collision guard ✅
- `cli_batch.py:496` — parallel dispatch with no conflict validation ✅
- `waves.py:49/56` — `_conflicts`/`plan_waves` present; `grep` confirms **zero production importers** (only 5 test files) ✅
- `meta.py:392` — meta dedupe block confirmed ✅
- `test_pi_batch.py:425` — parallel retry tests exist, none cover collisions ✅
- **One factual correction:** the direction claims `TaskNode` is "constructed only by device-fabric modules" — verified it's constructed **only by tests** (5 files); fabric modules only type-annotate it. Core claim ("never by the batch runner") stands.

**Key design decisions (all within the direction's scope):**
- **R1** — Fail-closed collision check at scheduling time (identical outputs, output-inside-other-cwd, nested outputs; stdout-only exempt), `OutputCollisionError` → exit 2 (house pattern), no agent invoked. Chose fail-closed over serialize-with-warning with explicit rationale: serializing same-file writers still deterministically loses data.
- **R2** — `run_parallel(waves=None)` optional `plan_waves`-style plan with per-wave barrier; plan validated (full coverage, intra-wave conflict-free) before any agent starts; retries/min_interval/budget-cap/validators preserved via reuse of `_run_parallel_one`/`_collect_parallel`.
- **R3** — New `pbatch/parallel_plan.py` adapter (`tasks_to_nodes`, `plan_waves_for` — the first production importer of `waves.py`); `waves.py`/`fabric_sched.py` untouched.
- **Placement constraint discovered:** `runner.py` is 996/1000 lines and only `pi-batch.py` is legacy-exempt — >1000 lines **fails** `make ci`'s quality gate, so all new logic goes in the new module (consistent with the repo's god-file split doctrine).
- **Acceptance checks preserved 1:1** as T1–T4 with concrete fixtures, sub-cases (incl. a corrected containment example), and existing test conventions (counter agents, `monkeypatch` sleep).
