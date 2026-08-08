# Requirements Specification — Parallel batch output-collision detection + wave-scheduling wiring (module pbatch)

| Field | Value |
|---|---|
| Module | `pbatch` |
| Direction id | `ba9cd1a4` |
| Source analysis | `docs/auto/analyses/pbatch-ba9cd1a4.json` (direction #1) |
| Status | Proposal (all 8 citations verified live; 1 factual correction noted in §2.2) |
| Scope | This direction only — no expansion |

---

## 1. Summary

`run_parallel` (pbatch/runner.py:935) submits tasks to a plain
`ThreadPoolExecutor` in submission order with no check that two tasks write
the same resolved output path, or that one task's output falls inside
another task's cwd. A tasks.yaml/stdin batch where two tasks share an output
races **last-write-wins** with no lock and no fail-closed rejection —
silently corrupting a deliverable under 7×24 unattended operation.

The conflict/dependency machinery built for exactly this —
`plan_waves`/`_conflicts`/`rank_nodes`/`assign_agents` in `pbatch/waves.py`
— has **zero production importers** (verified: only 5 test files import
`pbatch.waves`). Meta role outputs are collision-safe (sanitized names +
per-plan dedupe, pbatch/meta.py:380-395), which makes the plain parallel
batch path the notable exception.

This spec requires: (R1) a deterministic fail-closed collision check at
scheduling time on the plain parallel path — no agent invoked, exit code 2
with a report of every colliding group; (R2) an optional wave plan accepted
by `run_parallel` that co-schedules only non-conflicting tasks with a
per-wave barrier, so conflicting tasks never run concurrently; (R3) a
minimal Task→TaskNode adapter that gives `waves.py` its first production
importer without touching `waves.py` internals. The four supplied acceptance
checks are preserved and made concrete in §5.

**Placement constraint (verified, §8):** `pbatch/runner.py` is 996 lines
against the 1000-line budget enforced by `quality.py` (`make ci` gate;
only `pi-batch.py` is legacy-exempt). All new logic therefore lives in a
new module, `pbatch/parallel_plan.py`; `runner.py` receives only a thin
import + call sites + the `waves` parameter.

---

## 2. Evidence verification

### 2.1 Cited references — verified against the current repository

| Cited evidence | Verification result |
|---|---|
| `pbatch/runner.py:935` — `run_parallel` | ✅ **Confirmed at line 935.** `ThreadPoolExecutor(max_workers=workers)` created at **:947**; no output-path uniqueness check anywhere in the function body (935-970). |
| `pbatch/runner.py:956` — `pool.submit` in submission order | ✅ **Confirmed at line 956.** `fut_map[pool.submit(_run_parallel_one, t, i, ...)] = t` in `for i, t in enumerate(tasks, 1)` — plain submission order, no conflict/dependency ordering. |
| `pbatch/runner.py:886` — `_run_parallel_one` retry loop | ✅ **Confirmed at line 886.** Retry loop with `_retry_wait` backoff, validator-feedback (`_retry_task_with_feedback`), `SystemExit(3)` budget marker; no collision guard. |
| `pbatch/cli_batch.py:496` — parallel dispatch | ✅ **Confirmed at line 496.** `_execute_batch` (**:487**) calls `run_parallel(tasks, args.workers, validate_cmd=cli_validate, retries=..., ...)` with no conflict validation. |
| `pbatch/waves.py:49` — `_conflicts` write-set logic | ✅ **Confirmed at line 49.** `W_a ∩ (R_b ∪ W_b) ≠ ∅ or W_b ∩ R_a ≠ ∅`. |
| `pbatch/waves.py:56` — `plan_waves` | ✅ **Confirmed at line 56.** Returns waves of node ids; full-conflict wave serializes first node (**:72-73**) to avoid livelock. |
| `pbatch/waves.py` zero production importers | ✅ **Confirmed.** `grep -rn "import waves" pbatch/` returns **nothing**; the only importers are `tests/test_final_formulas.py:17`, `tests/test_tools_health.py:15`, `tests/test_decision_states.py:19,143`, `tests/test_runtime_governance.py:21`, `tests/test_lifecycle_metrics.py:20`. |
| `pbatch/meta.py:392` — meta role dedupe | ✅ **Confirmed (block :380-395).** Sanitized `safe_name` deduplicated with a sha1 suffix instead of last-writer-wins; `used_names.add(safe_name)` at :392. |
| `tests/test_pi_batch.py:425` — parallel retry tests | ✅ **Confirmed at line 425.** `test_parallel_retry_recovers_after_transient_failure` (:425-439) plus :441-560 parallel retry/min-interval/CLI tests; **none** covers output collisions. |

### 2.2 Factual correction to the direction

The direction states `fabric_sched.TaskNode` "is constructed only by
device-fabric modules". Verified: **`TaskNode(...)` is constructed only by
tests** — `tests/test_final_formulas.py:22,51-52`,
`tests/test_decision_states.py:23,144-145`,
`tests/test_runtime_governance.py:101,197-200,212`,
`tests/test_lifecycle_metrics.py:132`, `tests/test_fabric_sched.py:16`.
The device-fabric modules (`pbatch/fabric_sched.py` and the `fabric_*`
modules) reference `TaskNode` only in the class definition (:20) and type
annotations; they never construct it. The direction's core claim — "never
by the batch runner" — is confirmed: construction sites are even narrower
than stated (test-only, not device-fabric). Non-material for the design.

### 2.3 Additional verified facts used by this spec

- `run_parallel` has exactly **3 production callers**, all passing no
  conflict information: `pbatch/cli_batch.py:496` (single-batch
  `--mode parallel`), `pbatch/pipeline.py:559` (stage `mode: parallel`),
  `pbatch/campaign_analysis.py:149` (analysis jobs).
- `Task.output_path()` (pbatch/models.py:145-156) already resolves relative
  outputs against the task's cwd and calls `.resolve()` — the collision
  check can rely on fully resolved absolute paths.
- `Task.workdir()` (pbatch/models.py:142-143) = `self.cwd or os.getcwd()`.
- Repo Python floor is `>=3.9` (pyproject.toml:9) — `Path.is_relative_to`
  (3.9+) is available for containment checks.
- Exit code **2** is the house fail-closed pattern for invalid batch input
  (`pbatch/assessor.py:342`, `pbatch/eval.py:76-100`, `pbatch/rule_cli.py:30`,
  `pbatch/selfcheck.py:131`; AGENTS.md §2.1: bad input is rejected before
  any agent is started).
- Existing parallel tests use counter-based fake shell agents
  (`tests/test_pi_batch.py:380-407` `_flaky_agent`) — the same fixture
  style proves "no agent invoked" by asserting the counter file is absent.

---

## 3. Problem statement (verified)

1. **Silent race, unattended.** Two batch tasks sharing one resolved output
   path (or nested outputs, or an output inside another task's cwd) run
   concurrently in the same `ThreadPoolExecutor`; whichever finishes last
   wins. Under 7×24 operation nothing rejects the batch, nothing logs a
   conflict, and the deliverable is nondeterministically corrupted.
2. **Dead machinery.** `plan_waves`/`_conflicts`/`rank_nodes`/
   `assign_agents` in `waves.py` implement exactly the conflict scheduling
   this needs (write-set intersection → defer to next wave; full-conflict
   wave → serialization), but nothing in `pbatch/` imports them.
3. **Inconsistent safety posture.** Meta-role outputs are deduped and
   collision-safe (meta.py:380-395); plain parallel batch tasks are not.
   The `waves` gap is the notable exception in an otherwise fail-closed
   runner.

---

## 4. Requirements

### R1 — Fail-closed output-collision detection at scheduling time (plain parallel path)

**R1.1** `run_parallel` gains a pre-check, executed **before** the
`ThreadPoolExecutor` is created and therefore before any agent process is
invoked. The pre-check compares tasks pairwise using fully resolved paths
(`Task.output_path()`, which already resolves against the task's cwd; and
`Task.workdir()`).

**R1.2 Collision definition.** A pair of tasks collides when any of:

- **R1.2a** their resolved output paths are identical (file equality);
- **R1.2b** one task's resolved output path is inside the other task's
  resolved cwd (`Path.is_relative_to`, including equality — a task writing
  to another task's working directory);
- **R1.2c** one task's resolved output path is an ancestor directory of the
  other's resolved output path (nested outputs — writing a directory that
  contains another deliverable).

**R1.3** Tasks with empty `output` (stdout-only, `output_path() is None`)
write no runner-managed file and never collide — with anything, including
each other.

**R1.4** On any collision, `run_parallel` raises a dedicated
`OutputCollisionError` (new, defined in the new module
`pbatch/parallel_plan.py`, §8) carrying a structured report: for each
colliding group, the task indices, a prompt excerpt, and the resolved
output/cwd paths. **No agent is invoked, no output file is created,
nothing is written.**

**R1.5** `pbatch/cli_batch.py` catches `OutputCollisionError` around the
`run_parallel` call (:496), prints the report as a clear error (colliding
task indices + paths, in the same diagnostic style as validator rejections),
and exits with **code 2** (house fail-closed pattern, §2.3). The other two
production callers (`pbatch/pipeline.py:559`, `pbatch/campaign_analysis.py:149`)
let the exception propagate — a stage or analysis round fails loudly
instead of corrupting a deliverable.

**R1.6 Design decision (rationale).** The acceptance check permits either
fail-closed *or* "explicitly serialized with a warning". This spec mandates
**fail-closed** as the only behavior for the plain path: serializing two
tasks that write the *same* file still yields deterministic overwrite and
data loss, just without the race — it only converts a nondeterministic bug
into a deterministic one, while a warning scrolls past under unattended
operation. Fail-closed matches the runner's existing doctrine (failed
signatures rejected, zero artifacts on failure, bad input → exit 2).

**R1.7** Complexity note: the pre-check is O(n²) pairwise comparison with
`n` = batch size (typically ≤ dozens). No optimization required; document
in the function docstring.

### R2 — Optional wave-plan co-scheduling in `run_parallel`

**R2.1** `run_parallel` accepts an optional parameter
`waves: Optional[list] = None`, whose value is a `plan_waves`-style plan:
a list of waves, each wave a list of node ids (strings). When `None`, the
existing submission-order behavior applies unchanged (plus R1's pre-check).

**R2.2** When `waves` is provided, execution is wave-barrier based: submit
all members of wave *k* to the pool, drain every future of wave *k* (via
the existing `_collect_parallel` semantics), then start wave *k+1*. Tasks
in different waves therefore never run concurrently; tasks in the same wave
are guaranteed conflict-free (R2.3), so **conflicting tasks never run
concurrently** — serialization across waves is deterministic, never racy.

**R2.3** Before any agent is invoked, the provided plan is validated:

- every task index (0..n-1, as node id `str(i)`) appears **exactly once**
  (missing or duplicated index → fail closed);
- each wave's members are **pairwise non-conflicting** under the R1.2 rules
  (intra-wave check);
- cross-wave conflicts are **allowed by design** — separating conflicting
  tasks into different waves is the entire purpose of the plan.

Any violation raises the same `OutputCollisionError` (no agent invoked,
exit code 2 via cli_batch).

**R2.4** Existing per-task semantics are preserved across waves, unchanged:
per-task retries/`retry_delay`/`backoff`, validator gates (`validate_cmd`)
with validator-feedback retries, `min_interval` throttling between
successful completions, budget-cap behavior (cap trips mid-wave → abort
immediately, do **not** start the next wave, `SystemExit(3)` +
`kill_active_procs`), and rejected-artifact handling. The wave mode must
not reimplement or weaken any of these — it reuses `_run_parallel_one` and
`_collect_parallel` per wave.

**R2.5** Edge cases: `workers=1` with a plan behaves identically to serial
execution (trivially satisfied); empty task list and/or empty plan → no-op,
no error; node ids that are not `str(i)` for any `i` → fail closed (R2.3
coverage rule).

### R3 — Minimal `waves.py` production wiring (adapter, no internals change)

**R3.1** New module-level function in the new module
`pbatch/parallel_plan.py`: `tasks_to_nodes(tasks) -> list[TaskNode]`,
mapping each `Task` to a `TaskNode` with:

- `id = str(i)` (the enumerate index — this is the documented contract
  that makes `plan_waves` output directly feedable to `run_parallel`);
- `goal` = prompt excerpt (first 60 chars, same truncation style as
  `print_summary`);
- `write_set = [str(resolved output path)]` when the task has an output,
  else `[]`;
- `read_set = []`, `depends_on = []` (batch tasks declare no reads or
  dependencies — the batch path is write-conflict scheduling only);
- all other `TaskNode` fields at their defaults.

**R3.2** New module-level function in `pbatch/parallel_plan.py`:
`plan_waves_for(tasks) -> list[list[str]]`, implemented as
`waves.plan_waves(tasks_to_nodes(tasks))`. This is the **first production
importer of `pbatch.waves`** (verified zero today, §2.1) and the one-liner
callers/tests use to obtain a plan. `pbatch/runner.py` imports
`parallel_plan` (for the pre-check and the `waves` parameter contract)
but the adapter/planning code itself stays out of the 996-line file.

**R3.3** `pbatch/waves.py` and `pbatch/fabric_sched.py` are **not modified**
— the pure functions stay pure and their existing 5 test files stay green.

### R4 — Documentation

**R4.1** `AGENTS.md` §2.2 (result validation) gains one bullet: parallel
batches with colliding output paths are rejected at scheduling time
(exit code 2, no agent invoked); `run_parallel(waves=...)` co-schedules
non-conflicting tasks with a per-wave barrier.

**R4.2** `run_parallel`'s docstring documents the pre-check, the `waves`
parameter contract (R2.1-R2.3), and the node-id mapping contract (R3.1).

---

## 5. Test requirements (acceptance checks, made concrete)

The four supplied acceptance checks are preserved one-for-one. Test style
follows existing conventions: counter-based fake shell agents
(`tests/test_pi_batch.py:380-407`), `monkeypatch.setattr(mod.time, "sleep",
...)` to neutralize backoff.

### T1 (acceptance 1) — `tests/test_pi_batch.py::test_parallel_output_collision_fails_closed_no_agent`

- Two `Task`s with the **same resolved output path** (one relative
  `"o.md"` and one absolute `str(tmp_path/"o.md")` with the same cwd, to
  exercise cwd-relative resolution); agent = counter script.
- `run_parallel(tasks, workers=2)` raises `OutputCollisionError` **before
  the pool exists**; the counter file **does not exist** (no agent invoked);
  no output file is created; the exception report names both task indices
  and the shared resolved path.
- Sub-cases (same assertion shape):
  - **T1b** containment: **no**-collision case — same basename, distinct
    dirs: task A `cwd=tmp_path/"a"`, `output=tmp_path/"a"/o.md`; task B
    `cwd=tmp_path/"b"`, `output=tmp_path/"b"/o.md` → no collision, both
    execute with `success=True`. Collision case — output inside the other
    task's cwd: task A `cwd=tmp_path`; task B `cwd=tmp_path/"b"`,
    `output=tmp_path/"b"/a.md` → B's output is inside A's cwd → collision
    (R1.2b).
  - **T1c** nested outputs: A output `tmp_path/"dir"`, B output
    `tmp_path/"dir"/"x.md"` → collision (R1.2c).
  - **T1d** stdout-only: two tasks with `output=""` plus one task with a
    distinct output → no collision; executes normally (results have
    `success=True`).
- CLI-level companion
  `tests/test_pi_batch.py::test_cli_parallel_collision_exit_2`: a tasks.yaml
  with two tasks sharing an output, `--mode parallel` → process exits 2,
  stderr names both colliding tasks and paths, and the counter agent was
  never invoked.

### T2 (acceptance 2) — `tests/test_pi_batch.py::test_parallel_wave_plan_co_schedules_non_conflicting`

- Three tasks: t0 → `out-a.md`, t1 → `out-b.md`, t2 → `out-a.md`
  (t2 conflicts with t0). Agent records start/end timestamps per task into
  a shared log file (barrier-observable), workers=2.
- `plan = mod.plan_waves_for(tasks)` → `[["0","1"], ["2"]]` (t2 deferred —
  matches `test_plan_waves_write_conflict_deferred` semantics).
- `run_parallel(tasks, workers=2, waves=plan)`:
  - both `out-a.md` and `out-b.md` exist with the content their task wrote;
  - the log proves t0 and t1 overlapped in wall-clock (same wave) while t2
    started only after t0 finished — **the two writers of `out-a.md` never
    ran concurrently** (acceptance: "conflicting tasks never run
    concurrently");
  - final `out-a.md` content is deterministic (equals t2's write).
- Companion negative test
  `test_parallel_wave_plan_invalid_fails_closed`: a plan placing t0 and t2
  in the same wave → `OutputCollisionError` before any agent invocation; a
  plan missing an index / duplicating an index → same.

### T3 (acceptance 3) — `tests/test_runtime_governance.py::test_plan_waves_fed_to_run_parallel_preserves_deliverables`

- Build `TaskNode`s directly with `write_set` = each task's resolved output
  path (reusing the file's existing `_node` helper, `:101`), call
  `plan_waves` (already imported at `:21`), then feed the resulting plan to
  `run_parallel` with matching `Task`s.
- Assert **all deliverable contents are preserved** (each distinct output
  file contains exactly what its task wrote; no last-write-wins race): with
  the write-conflict fixture from `test_plan_waves_write_conflict_deferred`
  (a→file-x, b→file-x, c→file-c), plan `[[a,c],[b]]` → `file-c` intact, and
  the overlap log shows a and c concurrent, b after a — **no concurrent
  writers of file-x**.
- This test lives in `test_runtime_governance.py` per the acceptance check
  and is the end-to-end proof that `plan_waves` output is directly
  feedable to `run_parallel` (R3.1 id contract).

### T4 (acceptance 4) — engineering gates

- `make ci` passes (387 regression tests + 25 eval cases + harness self-
  tests; the 5 existing `waves.py` test files and all existing parallel
  retry/min-interval tests stay green — `waves=None` path is unchanged
  except for the new pre-check, which cannot fire for the existing
  fixtures' distinct outputs).
- `python quality.py --strict .` passes. `runner.py` stays ≤1000 lines
  (currently 996 — this is why all new logic lives in the new
  `pbatch/parallel_plan.py` module, which itself must respect the
  function ≤50-line / file ≤1000-line budgets).

---

## 6. Non-goals (explicitly out of scope)

- **No new CLI flags** — no `--parallel-waves`, no `--collision-policy`.
  The waves path is exercised through the `run_parallel` API and tests;
  the plain path stays fail-closed by default.
- **No auto-planning in the plain path** — `run_parallel(waves=None)` must
  not silently switch to serialization; R1's fail-closed behavior is the
  default safety posture.
- **No changes to `pbatch/waves.py` or `pbatch/fabric_sched.py`** — pure
  functions and `TaskNode` stay untouched; only a new production importer
  (R3) is added.
- **No dependency/read-set expressiveness for batch `Task`** —
  `depends_on`/`read_set` are always empty in the adapter; batch wave
  planning is write-conflict scheduling only. (DAG dependency support in
  batch tasks is a different direction.)
- **No serialization-with-warning mode** — explicitly rejected in R1.6.
- **No cross-process/disk-level locking, no detection of collisions with
  pre-existing files on disk** — detection is intra-batch, at scheduling
  time, per the acceptance checks.

---

## 7. Verification matrix (evidence → requirement)

| Requirement | Evidence anchor | Acceptance check |
|---|---|---|
| R1.1-R1.3 collision pre-check | runner.py:935-970 (no check today), models.py:145-156 (`output_path` resolve) | T1 (check 1) |
| R1.4-R1.5 fail-closed, exit 2, no agent | cli_batch.py:496 (dispatch site), house exit-2 pattern (assessor.py:342, selfcheck.py:131) | T1 + T1 CLI companion (check 1) |
| R2.1-R2.3 optional wave plan, intra-wave validation | waves.py:56-80 (`plan_waves` semantics), waves.py:49 (`_conflicts`) | T2 + T2 negative (check 2) |
| R2.4 preserved semantics | runner.py:886-906 (`_run_parallel_one`), runner.py:909-933 (`_collect_parallel`) | existing parallel tests stay green (check 4) |
| R3.1-R3.3 adapter + first production importer | waves.py zero-import verification (§2.1) | T3 (check 3) |
| R4 documentation | AGENTS.md §2.2 | review of diff |

## 8. Change surface estimate

`runner.py` is 996/1000 lines and **only `pi-batch.py` is legacy-exempt**
(quality.py:29 `_LEGACY_OVERRUN_FILES`; >1000 lines is a failing
violation, quality.py:74-75, exit 1 via main) — so all new logic lives in
a new module, consistent with the repo's documented god-file split
doctrine (cli_rounds.py / cli_batch.py / campaign_exec.py splits):

- **`pbatch/parallel_plan.py` (new):** `OutputCollisionError`,
  `detect_collisions(tasks)` (R1.2 rules), `tasks_to_nodes` (R3.1),
  `plan_waves_for` (R3.2 — first production importer of `pbatch.waves`).
  Must itself pass `quality.py --strict` (≤50-line functions, ≤1000 lines).
- `pbatch/runner.py`: import `parallel_plan`; pre-check call before pool
  creation; `waves: Optional[list] = None` parameter + per-wave barrier
  loop (reuses `_run_parallel_one`/`_collect_parallel`); `run_parallel`
  docstring. Net addition kept small so the file stays ≤1000 lines.
- `pbatch/cli_batch.py`: catch `OutputCollisionError` around :496, print
  report, exit 2.
- `tests/test_pi_batch.py`: T1 (4 sub-cases + CLI companion), T2 (+ negative).
- `tests/test_runtime_governance.py`: T3.
- `AGENTS.md`: one bullet (§R4.1).
- No config (`pi-batch.yaml`), no `waves.py`, no `fabric_sched.py`, no
  pipeline/campaign signature changes (exceptions propagate).
