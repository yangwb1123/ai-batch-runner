All verification complete. Here is the QA lead review.

---

# QA Lead Review — pbatch parallel output-collision + wave-scheduling design (`pbatch-ba9cd1a4`)

**Scope reviewed:** `docs/auto/design/pbatch-ba9cd1a4-design.md` (544 lines) against current tree, requirements spec `docs/auto/requirements/pbatch-ba9cd1a4-requirements.md`, and the live implementation of `runner.py`, `cli_batch.py`, `cli_rounds.py`, `cli.py`, `pipeline.py`, `campaign_analysis.py`, `waves.py`, `models.py`, `meta.py`, `fabric_sched.py`.

## 0. Baseline verification (all design evidence claims re-checked live)

Every §0.1/§0.3 claim in the design was re-verified against the tree — all ✅:

| Claim | Verified |
|---|---|
| `run_parallel` at runner.py:935, `ThreadPoolExecutor` :947, submission order :956, retry loop :886, no collision guard | ✅ read 855–996 |
| `cli_batch._execute_batch` :487–500, no conflict validation | ✅ |
| `waves.py:49 _conflicts`, `:56 plan_waves`, :72–73 serialize-on-full-conflict | ✅ |
| Zero production importers of `waves` | ✅ only 5 test files import it |
| `meta.py:392` role-name dedupe | ✅ (meta runs its own `ThreadPoolExecutor` at meta.py:256, not `run_parallel` — out of scope, correctly) |
| test_pi_batch.py:425 parallel retry, none cover collisions | ✅ |
| `TaskNode` constructed only by tests | ✅ |
| runner.py = 996 lines; only `pi-batch.py` legacy-exempt (quality.py:29) | ✅ `wc -l` = 996 |
| Python ≥3.9 floor (pyproject.toml:9) | ✅ (local interpreter is 3.10.7) |
| Exit-2 house pattern | ✅ assessor.py:342, eval.py, rule_cli.py:30, selfcheck.py:131 |
| `Task.output_path()` resolves vs cwd, None for empty | ✅ models.py:145–156 |
| 3 production `run_parallel` callers (cli_batch:496, pipeline:559, campaign_analysis:149) | ✅ |
| Line accounting: 996 − 28 + 30 + 1 import = **999** | ✅ counted the reference body myself (30 lines vs old 28) |

**Propagation paths traced in code (design claims F1/F4/R1.5 verified):**
- Batch mode: `_execute_batch` → `sys.exit(2)` propagates through `_run_batch_round` → `_run_rounds` — **no `except` around `_execute_round`** (cli_rounds.py:66–102), so the 7×24 round loop cannot spin on a collision. ✅
- Pipeline/campaign: `_execute_stage_tasks` → `execute_stage` → `_run_pipeline_stage` → `run_pipeline` → `cli.main` — no catch for non-`SystemExit` exceptions → traceback + exit 1. ✅ Loud propagation confirmed.
- Budget-cap mid-wave: `_collect_parallel` on empty `fut_map` returns `([], True)` → `SystemExit(3)` before next wave. ✅
- `from_dir` stages use non-recursive glob (pipeline.py:283) → all tasks share `cwd=dir_path` → same-cwd shape, unaffected by C1 correction. ✅ Campaign analysis (campaign_analysis.py:103–104) also same-cwd, and `module_slug` includes a digest → collision-free by construction. ✅
- Existing parallel fixtures all use `tmp_path` outputs with process cwd → collision-free under the corrected predicate (F6). ✅ Only 5 `run_parallel` call sites in tests, all distinct-output.
- Incidental bonus: current `run_parallel.__doc__` is **None** (docstring placed after `workers = ...`, verified via AST) — the design's rework fixes this, which R4.2 actually depends on.

## 1. Test inventory and commands actually run for this revision

Commands run on this checkout (all current, no historical results reused):

| Command | Result |
|---|---|
| `python -m pytest tests/test_runtime_governance.py -q -k "waves or plan_waves"` | **4 passed** |
| `python -m pytest tests/test_pi_batch.py -q -k "parallel"` | **6 passed, 3 failed** (see finding E1) |
| `python -m pytest tests/test_final_formulas.py tests/test_tools_health.py tests/test_decision_states.py tests/test_lifecycle_metrics.py tests/test_fabric_sched.py -q` | **66 passed** (the 5 waves-adjacent files + TaskNode) |
| `python quality.py --strict pbatch/runner.py` | **OK** |
| AST inspection of `run_parallel` | docstring misplaced (see finding L1) |
| Static reads: runner.py, cli_batch.py, cli_rounds.py, cli.py, pipeline.py, campaign_analysis.py, waves.py, models.py, meta.py, fabric_sched.py, `__init__.py`, 2 test files | — |

Full `make ci` was **not** run: a live campaign instance (`pi-batch.py campaign --retry-failed --reuse`, pid 1547473, age 2.2 h) holds the run lock and fails CLI-subprocess tests with exit 5 regardless of code state (verified below). No design test exists yet — implementation is Phase 3 of the design's own migration plan.

## 2. Requirement-to-test matrix

Status against the design's §6 acceptance mapping, with my verification of each planned test's feasibility:

| Req | Planned test | Status | Evidence |
|---|---|---|---|
| R1.1–1.4 pre-check, no agent | `test_parallel_output_collision_fails_closed_no_agent` + T1b/c/d | ✅ sound; **T1b collision sub-case under-specified** (F1) | counter-agent fixture exists (test_pi_batch.py:380); no-agent proof pattern proven |
| R1.5 exit 2, stderr | `test_cli_parallel_collision_exit_2` | ✅ sound; inherits lock flake (E1) | `logging.basicConfig` → stderr (F5) verified; `_inputs_dir` helper exists |
| R2.1–2.2 waves + barrier | `test_parallel_wave_plan_co_schedules_non_conflicting` | ✅ sound; deterministic (plan `[["0","1"],["2"]]` re-derived from waves.py algorithm) | plan_waves order = list order; barrier = drain-then-next in reference loop |
| R2.3 plan validation | `test_parallel_wave_plan_invalid_fails_closed` | ⚠️ incomplete — type-shape cases unhandled (F2) | see finding F2 |
| R2.4 preserved semantics | existing retry/min_interval/validator tests stay green | ✅ for plain path; **wave-mode budget cap untested** (F3); **min_interval per-wave gap** (F4) | traced `_collect_parallel` per-wave call |
| R2.5 edges | T2 negatives (empty, bad ids, C4 empty-plan) | ✅ except `workers=1`+plan untested (L3) | — |
| R3.1–3.2 adapter + first importer | `test_plan_waves_fed_to_run_parallel_preserves_deliverables` | ✅ sound; wave `[0,2]` verified collision-free under `collides()` | test_runtime_governance.py:21 already imports plan_waves; `_node` helper at :100 |
| R3.3 waves untouched | `git diff --stat` review | ✅ gate; not a test | — |
| R4 docs | AGENTS.md bullet + docstrings | ⚠️ R4.2 is a pointer, not the full contract (documented deviation, acceptable) | C3 line budget; docstring fix is real (L1) |
| Engineering gates | `make ci` + `quality.py --strict` | ⚠️ runner lands at **999/1000 — 1 line headroom** (L2) | my count confirms |

## 3. Findings (sorted by severity)

### F1 — Medium: T1b collision sub-case as written will not collide
**Untested failure / test-spec defect.** The sub-case says: "task A `cwd=tmp`; task B `cwd=tmp/b`, `output=tmp/b/a.md` → collision". `collides()` returns `None` immediately when either `output_path()` is `None` — A's output is never specified, so the pair is **not** a collision and the assertion fails. The containment check is only reached with two non-None outputs.
**Exact test to add (fix):** give A an explicit distinct output, e.g. A `output=tmp/o-a.md`, B `cwd=tmp/b`, `output=tmp/b/a.md`.
**Acceptance assertion:** `detect_collisions([A,B])` returns one group with reason `"output inside the other task's cwd"`; plus the equality sub-case A `output=tmp/b`, B `cwd=tmp/b`, `output=tmp/c.md` → also flagged (output == other cwd; `is_relative_to` equality — implemented but unpinned).

### F2 — Medium: fail-closed claim overreaches on plan *shape* validation
**Untested failure.** `_validate_plan` assumes `waves` is `list[list[str]]`. Verified holes in the reference implementation:
- `waves=5` (or any non-iterable) → `TypeError` (traceback, exit 1), not `OutputCollisionError`.
- `waves="01"` for a 2-task batch → **silently accepted**, iterating characters as waves `[["0"],["1"]]` — an accidental pass, not fail-closed.
- Python ≥3.11: `int("9"*5000)` raises `ValueError` (int max-str-digits, default 4300) — unhandled, crashes instead of `OutputCollisionError`. Repo floor is ≥3.9, so 3.11/3.12 hosts are in scope.
**Exact test to add:** `test_parallel_wave_plan_bad_shapes_fail_closed` — `run_parallel(tasks, waves=5)`, `waves="01"`, `waves=[[0,1]]`, `waves=[["9"*5000]]` each raise `OutputCollisionError` (no agent).
**Acceptance assertion:** `pytest.raises(OutputCollisionError)` for all four; counter file absent. Fix: wrap id parsing in `try/except (TypeError, ValueError)` and require `isinstance(wave, list)`.

### F3 — Medium: wave-mode budget cap ("no next wave") is new logic with zero coverage
**Untested failure.** The `if capped: raise SystemExit(3)` placement *inside* the wave loop is the R2.4 "do not start the next wave" guarantee. Existing T8 budget tests exercise only the plain path.
**Exact test to add:** `test_parallel_wave_budget_cap_stops_next_wave` — budget=1 (via `budget_try_consume` monkeypatch or metering config), plan `[["0","1"],["2"]]`; wave 1 runs, cap trips → `SystemExit(3)`; task 2's counter file absent; no agent for task 2.
**Acceptance assertion:** `pytest.raises(SystemExit)` with `e.value.code == 3`; task-2 counter absent; task-1/0 outputs intact.

### F4 — Medium: `min_interval` is per-wave, not global — delta from R2.4 text
**Regression risk / behavior gap.** `_collect_parallel` resets its success counter per call, so a success at the end of wave *k* and the first success of wave *k+1* are **not** throttled (back-to-back across the barrier). R2.4's literal "min_interval throttling between successful completions" covers the boundary.
**Decision needed:** either carry a cumulative success count across waves (two-line change: pass `successes` in/out) or explicitly document "per-wave throttle" in the contract. If kept per-wave, add a contract note; if global, test:
**Exact test:** `test_parallel_wave_min_interval_across_barrier` — 2-wave plan, agents instant, `min_interval=1.0`, sleep-log assertion `sleeps >= [1.0]` (cumulative) vs `== []` (documented per-wave).
**Acceptance assertion:** matches whichever semantics the contract states; must not be silent.

### F5 — Medium: pipeline/campaign propagation (R1.5) has no integration test
**Untested failure.** F4/R1.5 claims "loud failure, zero corruption" for pipeline and campaign callers, but only the CLI exit-2 path gets a test. Nothing pins that a colliding `mode: parallel` stage raises (not retries, not corrupts) and that earlier-stage artifacts are untouched.
**Exact test to add:** `test_pipeline_parallel_stage_collision_fails_loud` — Stage `mode="parallel"`, two tasks sharing an output, counter agent → `pytest.raises(OutputCollisionError)` from `execute_stage`; counter absent; no output file. (Optionally a campaign-level variant.)
**Acceptance assertion:** exception raised before any agent; zero files written.

### E1 — Environmental: 3 CLI parallel tests are red on this checkout (run-lock)
**Flake risk, pre-existing, not design-caused.** Verified failure: `test_cli_parallel_retries`, `test_cli_parallel_min_interval`, `test_cli_shared_session_parallel_rejected` all exit 5 — "Another pi-batch instance is running (pid=1547473 … campaign --retry-failed --reuse …)". A live campaign holds the run lock; CLI-subprocess tests don't pass `--no-lock`. **The new `test_cli_parallel_collision_exit_2` will inherit exactly this flake** (it asserts returncode 2 but will get 5 whenever a campaign/7×24 loop is running). Recommendation: the new test should pass `--no-lock` (or fix the shared CLI test harness separately); log as CI/manual-suite gap.

### L1 — Low: incidental docstring fix is required for R4.2
Current `run_parallel.__doc__` is `None` (docstring after assignment). The design's rework fixes it — note it in the changelog; R4.2's "docstring documents the contract" is satisfied only because of this fix.

### L2 — Low: 1-line headroom at 999/1000
Any reviewer edit to runner.py in the same change (a comment, a type hint) trips the `make ci` quality gate. Adopt the design's fold option (`if capped: raise SystemExit(3)` on one line → 998) for margin.

### L3 — Low: untested edge cases (cheap pins)
- **F7 symlink aliasing** — claimed behavior (resolve follows symlinks → flagged) with no test: create `tmp/link/o.md → tmp/real/o.md`, tasks output to both → `OutputCollisionError`. **Add.**
- `workers=1` + multi-wave plan (R2.5 "identical to serial") — one assertion, no race possible.
- Three-task triangle (t0/t1, t1/t2 collide) — asserts the report lists *every* pair (R1.4 "every colliding group").
- Shuffled valid plan `[[2],[0,1]]` executes in plan order — pins that plan order, not submission order, governs waves.
- 0-based indices in the CLI report vs 1-based tasks.yaml numbering — minor UX; consider `tasks {a+1}` in `_report` or print both.
- Wave-mode PROGRESS log goes backward across waves (`_collect_parallel` logs `k/N` per wave: "2/3" then "1/3") — pass `len(wave)` as total. Low observability under 7×24 log monitoring.

### Documented non-issues (accepted, no action)
- C1/C4 deviations from spec text are deliberate, flagged, and grounded (verified: same-cwd is the runner's own batch shape; `from_dir` is non-recursive so nested-cwd from_dir regressions cannot occur).
- F8 truncated `max_wave` plans, F9 case-insensitive FS, F6 `--dry-run` gap, F5 `--reuse` masking: all documented; F6's 3-line follow-up (collision check in `_print_dry_run`) is worth taking now — it's the cheapest way to make `--dry-run` honest.
- `waves.py`/`fabric_sched.py` untouched + 66 adjacent tests green: verified.

## 4. Prioritized scenario list

**Happy path** (must pass first):
1. Plain batch, distinct outputs, same cwd → unchanged behavior, all succeed (existing parallel tests = regression net).
2. `plan_waves_for` → `[["0","1"],["2"]]` → run succeeds, all deliverables correct (T2).
3. Raw `plan_waves` fed to `run_parallel` (T3): a+c concurrent, b after; `file-c` intact; `file-x` == b's write.
4. Stdout-only batch (T1d): no collision, executes.

**Boundary:**
5. Same basename, distinct cwds → no collision (T1b no-collision).
6. Same cwd, distinct outputs (campaign/from_dir shape) → no collision — the C1 regression pin.
7. Empty tasks `[]` (both modes) → `[]`; empty plan + non-empty batch → fail closed (C4).
8. `workers=1` + plan (L3); shuffled valid plan order.

**Error:**
9. Identical outputs, relative vs absolute form (T1 main).
10. Nested outputs (T1c); output-in-other-distinct-cwd **with A's output specified** (F1 fix); output == other cwd equality.
11. Invalid plans: missing index, duplicated index, bad id (`"x"`, `"01"`, `"9"` for n=3), **bad shapes** (F2), >4300-digit id on 3.11+.
12. CLI exit 2 + stderr report naming both tasks/paths (T1 companion, with `--no-lock`).

**Race:**
13. T2 barrier: t0/t1 overlap in wall-clock; t2 starts after t0 ends (add `sleep 0.2` to the fake agent — nanosecond-resolution assertions on instant agents are brittle); final `out-a.md` deterministic.
14. Wave-mode budget cap → no next wave (F3).
15. Symlink alias pair → flagged before any agent (F7, L3).

**Recovery:**
16. Retries inside a wave (existing `test_parallel_retry_recovers...` semantics reused via `_run_parallel_one`).
17. Pipeline stage collision → raises, no artifacts, earlier stages untouched (F5).
18. `--reuse` + collision: reused+validated tasks dropped pre-check, remaining set executes (F5 design intent — document, optionally pin with a test).

## 5. CI/manual-suite gaps, flake risks, fixtures, exit criteria

**CI gaps (add to Phase 3):** F2 shape-validation, F3 wave budget cap, F4 cross-wave min_interval, F5 pipeline propagation, F7 symlink, L3 triangle/order pins.

**Flake risks:**
- New CLI collision test inherits the run-lock flake (E1) — use `--no-lock` or fix the shared CLI harness.
- T2/T3 wall-clock overlap assertions — fake agents must sleep ≥100 ms; keep `workers` explicit (2).
- Runner line budget 999/1000 — adopt the fold for margin (L2).

**Fixtures needed (new):**
- `_overlap_agent` — logs `start,end` per task to a shared file with an internal sleep (T2/T3); reuse existing `_flaky_agent`/`_recording_agent` conventions (test_pi_batch.py:380–407).
- Symlink-output builder for F7.
- Colliding tasks.yaml builder (extend `_inputs_dir` style) for the CLI companion.

**Exit criteria for this revision:**
1. All Phase-3 tests green including the F1–F5 additions above; the 5 waves-adjacent files (66 tests) untouched and green.
2. `make ci` full pass with the campaign stopped (or `--no-lock` in CLI tests) — current 3 CLI failures are environmental, must be re-run in a lock-free environment to claim green.
3. `python quality.py --strict .` — runner ≤1000 (target 998), `parallel_plan.py` clean.
4. `git diff --stat` = `parallel_plan.py` (+new), `runner.py` (+3), `cli_batch.py` (+5–6), 2–3 test files, `AGENTS.md`; `waves.py`/`fabric_sched.py`/`__init__.py` byte-identical.
5. Manual spot-check: `python pi-batch.py tasks.yaml --mode parallel` (colliding) → exit 2, stderr names both tasks; `--dry-run` shows the batch (documented F6 gap).

**Verdict:** design is sound and the reference implementation is trace-verified end to end; no Critical/High findings. Five Medium findings (F1–F5) are all test-spec or hardening gaps rather than architecture defects — F1 must be fixed before Phase 3 tests are written (the sub-case as written would land a failing test), and F2 is a genuine hole in the feature's own fail-closed guarantee that should be fixed in the implementation, not just tested around.
