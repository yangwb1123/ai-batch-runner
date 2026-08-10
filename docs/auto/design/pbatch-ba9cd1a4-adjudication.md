# Owner Adjudication — design_gate sign-off (direction `ba9cd1a4`)

| Field | Value |
|---|---|
| Module | `pbatch` |
| Direction id | `ba9cd1a4` |
| Stage | design_gate (owner adjudication before Phase 2 implementation) |
| Inputs | requirements `pbatch-ba9cd1a4-requirements.md`, design `pbatch-ba9cd1a4-design.md`, architect review, qa_lead review |
| Verdict | **PASS** — implementation-ready under architect Option B + qa_lead F1–F5 + owner mandates M1–M4 below |

**Live verification performed for this adjudication** (not re-quoted from reviews): pipeline propagation chain `_execute_stage_tasks` (pipeline.py:559) → `execute_stage` (:362) → `_run_pipeline_stage` (:828) → `run_pipeline` → `_run_pipeline_round` (cli_batch.py:250) has **no catch** for non-`SystemExit` exceptions — traceback + exit 1 confirmed; `_collect_parallel` has **exactly one caller** (runner.py:958) — signature threading is safe; `quality.py` gate is `>1000` fails (`MAX_FILE_LINES=1000`, quality.py:24/74) — 1000 passes; `pipeline.py` is **988/1000** (12 lines headroom — same line-gate pressure the design fought for runner.py); `docs/ARCHITECTURE.md:88` still claims ≤500 (drift vs 1000 gate — architect #6 confirmed); `docs/architecture/DIRECTORY_MAP.md` absent, stale ref lives in `prompts/README.md` (architect #7 confirmed); `print_summary` public via `pbatch/__init__.py:11,32` (C3 grounding confirmed).

---

## 1. Spec deviations C1–C4 — owner sign-off (all four ACCEPTED)

| # | Deviation | Owner decision | Grounding (verified) | Condition / pin |
|---|---|---|---|---|
| **C1** | R1.2b containment checked only between **distinct** cwds | **ACCEPT** | Deliverables are runner-written atomically (mkstemp+rename, runner.py:356-360/580-588) — same-cwd batches have no deliverable race; same-cwd is the runner's own batch shape (campaign analysis `cwd=str(root)`, `from_dir` `cwd=fpath.parent`, default-cwd tasks.yaml — all verified); the literal rule contradicts the spec's own T3 | Nested **distinct** cwds remain flagged (the genuinely new exposure); same-cwd regression sub-case added to T1; residual shared-cwd side-effect risk accepted as baseline (architect #5 sentence goes in the AGENTS.md bullet: heuristic, not sandbox) |
| **C2** | `plan_waves_for` tightens raw `plan_waves` output under `collides()` | **ACCEPT** — required for correctness | Write-set intersection (waves.py:49) cannot express path containment; raw output would fail the runner's own validation; blocked-defer doctrine matches waves.py:60-73; terminates (single-member waves always valid) | `waves.py`/`fabric_sched.py` stay byte-identical (R3.3); T3 pins the adapter end-to-end |
| **C3** | Unified wave loop (plain = single wave) instead of thin branch | **ACCEPT** | Verified arithmetic: naive branch +7 → 1003 (gate fail); `print_summary` is public API, not movable; unified loop = 999 (998 after the Option B fold) | `make ci` line gate is the hard acceptance; fold adopted proactively (architect #8) |
| **C4** | Empty plan + non-empty batch → **fail closed**, not silent no-op | **ACCEPT** | R2.5's "empty plan → no-op" contradicts R2.3 coverage doctrine; silent no-op would report success for a batch that ran nothing | `tasks=[]` remains a no-op in both modes (preserved); T2 negative pins the fail-closed case |

All four are recorded here as owner-acknowledged decisions, not silently absorbed. None changes R1–R4's substance; all four make the spec executable against the verified tree.

---

## 2. Architect §5 unknowns — owner resolutions (concrete)

### U1+U2 — future `--parallel-waves` CLI semantics & the fail-closed vs auto-serialize asymmetry

**Decision: fail-fast is the permanent default; waves stay API-only in this direction; auto-serialize is never silent and never a default.**

1. **No CLI wiring now** (spec non-goal §6; design §7). `run_parallel(waves=...)` is exercised via API + tests only.
2. **The asymmetry is intentional and becomes documented contract** (architect finding #1): plain path fail-closed (R1.6 doctrine — serializing same-file writers is deterministic data loss, not a fix); wave path allows cross-wave conflicts *by design* (separating conflicts into waves is the point of a plan). AGENTS.md bullet gains the sentence: *"wave plans may contain cross-wave conflicts by design."*
3. **Pre-decided semantics for a future `--parallel-waves` flag** (so it is never an accident): explicit opt-in only; requires `--mode parallel`; runs `plan_waves_for`; logs the plan plus a loud WARNING naming each task whose wave was deferred (i.e., what would have been exit-2 under the plain path); and — per R1.6 — **refuses to serialize identical-output writers**: a plan that merely converts a data-loss race into deterministic overwrite must fail closed, so the flag only ever resolves *separable* conflicts, never converts data-loss batches into "successful" runs.
4. **Trigger condition for the follow-up**: a real batch that needs wave co-scheduling (none exists today — the scheduler is dormant because nothing needs it).

### U3 — pipeline collision handling

**Decision: keep "let propagate" (spec R1.5) for Phase 2 — zero changes to `pipeline.py`.**

- `pipeline.py` is **988/1000** lines: the same line-gate problem the design fought for runner.py applies to any catch added inside `execute_stage` (+7–10 lines is real risk).
- **Spin analysis** (the decisive point): a collision is a *permanent batch-definition error*, not a transient failure. Catching it inside `execute_stage` (the shared-session pattern at pipeline.py:395-400) makes it a round-failed stage → the 7×24 round loop re-runs the pipeline → the colliding stage fails identically every round → `--max-rounds 0` burns quota forever. Propagation exits the round loop immediately (traceback + exit 1, no agent ever invoked) — the same "hard error, not retryable round failure" doctrine the design deliberately applied to the single-batch exit-2 placement (catch below the round loop, cli_batch.py:496).
- The exception's report is self-contained (task indices + resolved paths) — diagnosable from the run log.
- **Recorded follow-up shape** (pre-decided, not open): if a real 7×24 hit shows diagnosability gaps, catch in `_run_pipeline_round` (cli_batch.py:250) → `log.error(report)` + `sys.exit(2)` — house exit code, no traceback, and `SystemExit` still escapes the round loop (no spin). That is the exact, bounded upgrade path.
- qa_lead **F5 test stays as written**: `pytest.raises(OutputCollisionError)` from `execute_stage`; counter absent; no output files.

### U4 — case-insensitive filesystem scope

**Decision: Linux/CI scope. Do NOT case-normalize in `collides`; F9 stays a documented limitation.**

- Normalizing (e.g. `casefold()`) would flag `A.md` vs `a.md` on **case-sensitive Linux** too — where they are genuinely distinct files the runner writes correctly (mkstemp+rename, distinct resolved paths). False positive = fail-closed on a non-error = breaks real batches. That is worse than a documented gap on macOS, which is not a supported CI target for this repo.
- The AGENTS.md bullet states the scope explicitly: *"case-variant paths on case-insensitive filesystems are outside this guard's scope (Linux CI); the check is a scheduling-time heuristic, not a write-safety sandbox."*
- If macOS support ever becomes a requirement, the fix is an OS-level case-sensitivity probe (`os.path.samefile` on a case-variant pair), not unconditional normalization — tracked, not built.

### (U5 — architect §5 #4) shared-cwd side-effect baseline

**Accepted as the design's stated baseline** (serial mode and the runner already assume it). Eventual hardening = per-task cwd isolation or deliverable locking — deliberately out of scope, tracked in the AGENTS.md bullet's "heuristic, not sandbox" sentence.

---

## 3. Implementation-ready confirmation — Option B + F1–F5 + owner mandates

**Confirmed: the design is implementation-ready under architect Option B plus the qa_lead F1–F5 fixes**, with the following consolidated, binding change list for Phase 2–4:

### From architect Option B (adopted in full)
- **B1** Fold `if capped: raise SystemExit(3)` to one line → runner lands at **998** (2 lines of headroom banked).
- **B2** Wave-mode budget-cap test (this is qa_lead F3 below).
- **B3** AGENTS.md bullet tightening: architect #1 sentence (cross-wave conflicts by design) + #5 sentence (heuristic, not sandbox) + U4 scope sentence + F5 reuse-masking note (reused+validated tasks are dropped pre-check — acceptable, documented).
- **B4** Docs fixes in the same Phase 4 pass: `docs/ARCHITECTURE.md:88` (≤500 → ≤1000) and the stale `docs/architecture/DIRECTORY_MAP.md` reference in `prompts/README.md` (→ `docs/ARCHITECTURE.md`).

### From qa_lead F1–F5 (all binding)
- **F1** (test-spec fix): T1b collision sub-case must give A an explicit distinct output (`output=tmp/o-a.md`); plus the equality sub-case (output == other task's cwd). Assert reason `"output inside the other task's cwd"`.
- **F2** (implementation fix, not just a test): `_validate_plan` must fail closed on plan **shape**: (a) `not isinstance(waves, list)` → `OutputCollisionError` (kills `waves=5`, `waves="01"` char-iteration); (b) `not isinstance(wave, list)` → same; (c) wrap id parsing in `try/except (TypeError, ValueError)` (kills 3.11+ `int("9"*5000)` `ValueError`). Test: `waves=5`, `waves="01"`, `waves=[[0,1]]`, `waves=[["9"*5000]]` each raise `OutputCollisionError`, counter file absent. Lives in `parallel_plan.py` — zero runner line-budget impact.
- **F3** (wave-mode budget cap): budget=1, plan `[["0","1"],["2"]]` → `SystemExit(3)`, task-2 counter absent, wave-1 outputs intact.
- **F4** (min_interval across the wave barrier): **owner decision — cumulative/global semantics** (matches R2.4's literal "between successful completions" and `run_serial`'s global throttle at runner.py:881-882). Implement by threading `successes` through `_collect_parallel` (its only caller is the wave loop — verified; param+return on existing lines, drop the local init → **zero net line cost**, runner stays 998). Contract note added to the `run_parallel` docstring; test pins the boundary sleep (`sleeps >= [1.0]`).
- **F5** (pipeline propagation): per §2 U3 — `pytest.raises(OutputCollisionError)` from `execute_stage` with a colliding `mode="parallel"` stage; counter absent; no output file.

### Owner mandates (new, binding — all pre-discussed in the reviews)
- **M1** — Exception message wording: remove the hardcoded `(exit 2)` from `OutputCollisionError._report` (pipeline/campaign callers exit 1 — the claim is wrong there); `cli_batch`'s `log.error` appends "(exit 2)" itself. Report header states task indices are **0-based task-list positions** (L3 UX note).
- **M2** — Wave-mode PROGRESS: pass `len(wave)` as `_collect_parallel`'s `total` (no backward counter across waves; zero-line cost).
- **M3** — `--dry-run` honesty (design §7 F6, qa_lead endorsed): in `_print_dry_run`, after the task list, call `detect_collisions(tasks)` and print a `COLLISION WARNING: the real run would fail (exit 2) — <groups>` block; **keep exit 0** (dry-run stays informational — changing its exit contract would break existing scripts; the warning is the honesty). ~3 lines in cli_batch.py (500 → ~509, budget fine).
- **M4** — Test hygiene (qa_lead E1): the new `test_cli_parallel_collision_exit_2` must pass `--no-lock`; the 3 currently-red CLI parallel tests are environmental (live campaign holds the run lock) — `make ci` green is claimed only in a lock-free environment.
- **L3 cheap pins — accepted**: symlink-alias pair flagged (F7), `workers=1` + multi-wave plan, three-task triangle (report lists every pair), shuffled valid plan executes in plan order.
- **Declined**: `InvalidPlanError(OutputCollisionError)` subclass (architect #3) — single fail-closed type is defensible; every catch site treats it uniformly.

### Final diff surface (updated from design §5)
`parallel_plan.py` (+new, ~160 lines incl. F2 shape checks) · `runner.py` (→**998**) · `cli_batch.py` (+8–9 → ~509) · `pipeline.py` (**+0** — U3) · 2–3 test files · `AGENTS.md` · `docs/ARCHITECTURE.md:88` · `prompts/README.md` stale ref. `waves.py`, `fabric_sched.py`, `__init__.py` byte-identical.

---

## 4. Exit criteria (Phase 3/4, lock-free environment)

1. All Phase-3 tests green including F1–F5, M1–M4, and the L3 pins; the 5 waves-adjacent files (66 tests) untouched and green.
2. `make ci` full pass with the campaign stopped (or `--no-lock` in CLI tests) — current 3 CLI failures are environmental (run-lock), must be re-run lock-free to claim green.
3. `python quality.py --strict .` green — runner **998** ≤ 1000, `parallel_plan.py` clean.
4. `git diff --stat` matches the surface above; `waves.py`/`fabric_sched.py`/`__init__.py` byte-identical.
5. Manual spot-checks: colliding `tasks.yaml --mode parallel` → exit 2, stderr names both tasks+paths, counter agent never invoked; `--dry-run` on same → collision warning, exit 0; colliding pipeline stage → traceback + exit 1, zero artifacts (documented U3 behavior).

---

VERDICT: PASS - all four spec deviations C1-C4 signed off as owner decisions with verified grounding and pins; architect unknowns resolved (fail-fast permanent default, waves API-only with pre-decided follow-up flag semantics, pipeline collisions propagate by design with a recorded bounded upgrade path, case-insensitive FS scoped to Linux CI without normalization); design confirmed implementation-ready under Option B + F1-F5 + mandates M1-M4, with the line gate secured at runner 998 and pipeline.py untouched.
