# Design — Parallel output-collision detection + wave-scheduling wiring (module pbatch)

| Field | Value |
|---|---|
| Module | `pbatch` |
| Direction id | `ba9cd1a4` |
| Requirements | `docs/auto/requirements/pbatch-ba9cd1a4-requirements.md` (evidence-verified) |
| Status | Design (all evidence claims re-verified live; 3 design corrections in §0.2) |
| Scope | R1–R4 in scope; no expansion |

---

## 0. Evidence verification (all claims re-checked live)

### 0.1 Requirements claims — reproduced against the current tree

| Evidence claim | Verification result |
|---|---|
| `runner.py:935` `def run_parallel`; `:947` `ThreadPoolExecutor`; `:956` `pool.submit` in submission order; `:886` `_run_parallel_one` retry loop; no collision guard anywhere | ✅ exact (read `pbatch/runner.py` 860-962) |
| `cli_batch.py:496` parallel dispatch with no conflict validation (`_execute_batch` at :487) | ✅ exact |
| `waves.py:49` `_conflicts`; `:56` `plan_waves`; `:72-73` full-conflict wave serializes first node | ✅ exact |
| `waves.py` zero production importers | ✅ `grep -rn "waves" pbatch/` → only 5 test files: test_final_formulas, test_tools_health, test_decision_states, test_runtime_governance, test_lifecycle_metrics |
| `meta.py:392` role-name dedupe (`used_names.add` at :392; sha1 suffix disambiguation :380-395) | ✅ exact |
| `tests/test_pi_batch.py:425` parallel retry tests; none cover collisions | ✅ exact (`test_parallel_retry_recovers...` :425-439; min-interval/CLI tests :460-560) |
| `TaskNode(...)` constructed only by tests (5 files); fabric modules only type-annotate | ✅ exact — construction sites: test_final_formulas:22,51-52, test_decision_states:23,144-145, test_runtime_governance:101,197-200,212, test_lifecycle_metrics:132, test_fabric_sched:16 |
| `runner.py` = 996 lines vs 1000 budget; only `pi-batch.py` legacy-exempt | ✅ `wc -l` = 996; `quality.py:29` `_LEGACY_OVERRUN_FILES = {"pi-batch.py"}`; `:74` fail when `len(lines) > 1000` |
| `pyproject.toml:9` `requires-python = ">=3.9"` → `Path.is_relative_to` available | ✅ exact |
| Exit-2 house fail-closed pattern | ✅ assessor.py:342, eval.py:76-100/262/280, rule_cli.py:30, selfcheck.py:131 |
| `Task.output_path()` resolves relative outputs against task cwd, returns None for empty output | ✅ `models.py:145-156` |
| 3 production `run_parallel` callers: cli_batch:496, pipeline:559, campaign_analysis:149 | ✅ exact |

### 0.2 New findings beyond the evidence (design corrections — all within scope)

**C1 — The literal R1.2b containment rule is incompatible with the runner's own
production batch shapes, and contradicts the spec's own acceptance T3.**
R1.2b as written ("one task's resolved output path is inside the other task's
resolved cwd") flags **every pair of tasks that share a working directory and
write under it**. That is exactly how the runner constructs batches today:

- `campaign_analysis.py:104` — every analysis task gets `cwd=str(root)` with
  outputs `root/<output_dir>/analyses/<module>.json` (all under root);
- `pipeline.py:288` — `from_dir` stages: every task gets `cwd=str(fpath.parent)`,
  output next to the input in that same directory;
- single-batch tasks.yaml — tasks without `cwd` default to the process cwd
  (`Task.workdir()` = `os.getcwd()`), outputs relative to it.

Under the literal rule all three are dead on arrival (every pair collides),
which would break campaign analysis, `from_dir` pipeline stages, and plain
`--mode parallel` batches — the feature's own primary targets. Decisively,
**the spec's own acceptance T3 requires a same-cwd batch (a→file-x, b→file-x,
c→file-c) to be schedulable** — impossible under the literal rule.

**Correction:** containment is checked only between **distinct** cwds
(`cwd(a) != cwd(b)`). Rationale (grounded in §0.3 finding F1): deliverables are
runner-written via `mkstemp` + atomic `rename` at distinct resolved paths, so
same-cwd batches have no deliverable race — the agents' shared-directory side
effects are the accepted baseline that serial mode and the runner already
assume. With **strictly nested distinct** cwds, the inner task's working tree
lies inside the outer task's working tree: the outer agent's side effects
(git/build/test artifacts) can land on the inner task's deliverable paths — a
genuinely new exposure. T1b's nested-cwd collision case (A cwd=tmp, B
cwd=tmp/b) still fires; a same-cwd regression sub-case is added to T1.

**C2 — `waves.plan_waves` (write-set intersection) is strictly weaker than the
R1.2 predicate**, so a plan from `waves.plan_waves(tasks_to_nodes(tasks))` is
**not guaranteed** to pass `run_parallel`'s own intra-wave validation: R1.2b/c
containment cannot be expressed as write-set intersection (paths `P` and `P/x`
never intersect). `plan_waves_for` therefore calls `waves.plan_waves` (first
production importer, R3.2) and then **tightens**: re-checks each wave under
`collides()` and defers colliding members to later waves (same blocked-defer
doctrine as waves.py:60-73). Guaranteed to terminate: single-member waves are
always valid. This keeps `waves.py` untouched while making
`plan_waves_for`'s output always feedable.

**C3 — The spec's change-surface arithmetic is off by more than the line
budget allows.** The spec says runner.py gets "a thin import + call sites +
the `waves` parameter" and "stays ≤1000 lines" (996 today, 4 lines of
headroom). My exact line accounting (§2.2) shows a naive branch-based addition
needs +7 lines (→1003, gate failure); `print_summary` cannot be relocated (it
is public API via `pbatch/__init__.py:11,32`, imported by cli_batch/cli/
cli_rounds/pipeline). The design therefore **unifies the plain and wave paths
into one wave loop** (plain = a single wave of all tasks): net +3 lines + 1
import → **999 lines** (review round folds the cap-raise to land at
**998**, §2.2), with the full `waves` contract documented in
`parallel_plan.resolve_plan` and a pointer in `run_parallel`'s docstring
(R4.2 satisfied in substance; the inline contract is shortened — the 1000-line
gate is a hard `make ci` requirement).

**C4 — R2.5 ("empty plan → no-op, no error") contradicts R2.3's fail-closed
coverage rule for non-empty batches.** A caller passing an empty plan for a
non-empty batch would silently execute nothing and report success. Design
decision: **fail closed** (empty plan = coverage violation) — consistent with
the runner's fail-closed doctrine; `tasks=[]` still short-circuits to a no-op
in both modes. Two-line deviation, flagged for review.

**C5 — review round: the reference predicate's stdout-only early return
also skipped the containment check.** The original `collides` returned
None whenever either `output_path()` was None, so a task whose cwd is a
*nested distinct* directory containing the other task's deliverable was
never flagged — even though that is exactly the C1 "genuinely new
exposure" (the inner/outer agent's side effects can land on the
deliverable), and C1's rationale never depended on both tasks writing
outputs. Fixed: stdout-only tasks remain exempt from path-vs-path checks
but their **cwd participates in containment**; `collides` now only
exempts same-cwd pairs from containment and stdout-only *pairs* from
everything. Side effect: the qa_lead F1 under-specified T1b sub-case (A
stdout-only, B in a nested cwd) now collides on its own — the
containment reason fires without needing A's explicit output.

**C6 — review round: plan *shape* validation was incomplete (qa_lead
F2).** The reference `_validate_plan` assumed `list[list[str]]`:
`waves=5` raised TypeError (traceback, exit 1) instead of
`OutputCollisionError`; `waves="01"` iterated characters and was
silently accepted as `[["0"],["1"]]` for a 2-task batch; a >4300-digit
id raised the `int max-str-digits ValueError` — live on CPython
3.10.7+/3.9.14+ security backports (the repo's own floor version), not
just 3.11+. Fixed: `isinstance` guards on plan and wave shape plus a
digit-count guard (`len(nid) > len(str(len(tasks)-1))` ⇒ invalid) that
bounds the string before `int()`; every invalid shape now raises
`OutputCollisionError`. Verified by the review harness: all three holes
reproduced against the pre-fix reference, all closed post-fix.

### 0.3 Additional verified facts used by this design

- **F1 — Outputs are written by the runner, not the agent**: `_run_with_gate`
  and the validation path both use `tempfile.mkstemp` in the output's parent
  + `tmp.rename(out_path)` (runner.py:356-360, 580-588). Deliverable writes
  are atomic and race-free for **distinct** resolved paths; the true hazards
  are (a) identical resolved paths (last rename wins), (b) nested outputs
  (one task's file path is an ancestor of another's → mkstemp/rename
  interleavings corrupt), (c) an output inside another task's *different*
  cwd (that agent's side effects can clobber the deliverable).
- **F2 — `budget_allows` is imported from `.metering`** (runner.py:27); the
  executor loop must stay in runner.py (it needs `_run_parallel_one`,
  `_collect_parallel`, `budget_allows`, `kill_active_procs`). `parallel_plan`
  is a leaf module (imports only `models`, `fabric_sched`, `waves`) — no
  import cycle with runner.
- **F3 — `_collect_parallel` on an empty `fut_map`** (budget exhausted before
  any submit in a wave) returns `([], capped=True)` → `SystemExit(3)` raised,
  next wave never starts — exactly the R2.4 budget-cap requirement, for free.
- **F4 — `--reuse` filters tasks before `_execute_batch`** (cli_batch.py:291-
  295), so any `waves` plan is relative to the **post-filter** task list
  (API-level contract; no CLI path passes plans today).
- **F5 — `logging.basicConfig` (config.py:19) streams to stderr** — the T1
  CLI companion can assert the collision report on stderr.
- **F6 — Existing parallel fixtures are collision-free under the corrected
  predicate**: tests set no `cwd` (workdir = process cwd for all tasks) with
  distinct sibling outputs in `tmp_path` → same cwd (no containment check),
  distinct non-nested paths → no flag. All existing parallel tests stay green.

---

## 1. Design overview

Fail-closed output-collision detection + optional wave co-scheduling for
`run_parallel`, with the conflict scheduler in `waves.py` getting its first
production importer — without touching `waves.py` or `fabric_sched.py`.

```
cli_batch._execute_batch (--mode parallel) ──┐
pipeline._execute_stage_tasks (stage parallel) ─┼─► run_parallel(tasks, waves=None)
campaign_analysis.run_analysis_tasks ────────────┘        │
                                                          │ resolve_plan(tasks, waves)
                                                          ▼
                                              pbatch/parallel_plan.py  (NEW, leaf module)
                                              ├─ OutputCollisionError  (report + __str__)
                                              ├─ collides(a,b)         (R1.2 predicate, C1)
                                              ├─ detect_collisions()   (plain path, O(n²))
                                              ├─ resolve_plan()        (validate + return waves)
                                              ├─ tasks_to_nodes()      (Task → TaskNode, R3.1)
                                              └─ plan_waves_for()      (waves.plan_waves + tighten, R3.2+C2)
                                                          │
                                                          ▼
                                              unified wave loop in runner.py:
                                              plain path = single wave of all tasks (submission order);
                                              waves path = per-wave barrier, _collect_parallel per wave;
                                              budget cap mid-wave ⇒ SystemExit(3), no next wave (F3)
```

**R1** — plain path: pairwise collision pre-check via `resolve_plan` **before
the `ThreadPoolExecutor` exists**; on any collision raise
`OutputCollisionError`; `cli_batch` catches → report to stderr → exit 2; no
agent invoked, nothing written. Pipeline/campaign callers let it propagate
(loud failure, zero corruption).

**R2** — `run_parallel(tasks, waves=plan)`: plan validated before any agent
(full coverage, ids `str(i)`, intra-wave pairwise `collides()`-free; cross-wave
conflicts allowed by design), then wave-barrier execution reusing
`_run_parallel_one` / `_collect_parallel` unchanged (retries, validator
feedback, `min_interval`, budget cap, rejected-artifact handling all
preserved).

**R3** — `tasks_to_nodes` / `plan_waves_for` in the new module; `plan_waves_for`
is the first production importer of `pbatch.waves` and guarantees its output
passes R2.3 validation (C2 tightening).

**R4** — `AGENTS.md` §2.2 bullet + docstrings (contract in
`resolve_plan`/`parallel_plan` module docstring, pointer in `run_parallel`).

## 2. API changes

### 2.1 `pbatch/parallel_plan.py` (new module, ~150 lines)

```python
"""Parallel batch plan: fail-closed output-collision detection and the
wave-plan adapter that gives pbatch/waves.py its first production importer.

R1: run_parallel rejects batches whose tasks can corrupt each other's
deliverables before any agent starts (OutputCollisionError; exit 2 via
cli_batch). R2: run_parallel(waves=...) co-schedules conflict-free waves
with a per-wave barrier. R3: tasks_to_nodes/plan_waves_for adapt Task to
TaskNode/plan_waves without touching waves.py.

Collision predicate (R1.2, as corrected by design C1 + review C5):
identical resolved output paths; nested output paths (one is an ancestor
of the other); an output inside the OTHER task's cwd, checked only
between DISTINCT resolved cwds — batches sharing a working directory are
the runner's standard shape (campaign analysis, from_dir stages) and
their deliverables are runner-written atomically at distinct resolved
paths. A stdout-only task never collides via its own output, but its cwd
still participates in the containment check (C5): a nested distinct cwd
can contain another task's deliverable whether or not the inner task
writes one itself. Comparisons go through os.path.normcase (identity on
POSIX — Linux CI is byte-identical; Windows case-variant paths are
flagged; macOS APFS case-insensitive volumes remain a documented
limitation, F9).

Fail-closed plan validation (review F2): waves must be a list of lists
of canonical node ids (str(i), 0 <= i < len(tasks)); any other shape
(int, string, tuple, non-list wave, duplicate/missing/out-of-range id,
or an id too long to be canonical) raises OutputCollisionError — never
TypeError/ValueError. Ids are length-bounded before int() so absurdly
long digit strings cannot trip CPython's int max-str-digits ValueError
(4300 digits; live on 3.10.7+/3.9.14+ security backports too, not just
3.11+).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .fabric_sched import TaskNode
from .models import Task
from .waves import plan_waves as waves_plan_waves


class OutputCollisionError(Exception):
    """Fail-closed marker: colliding parallel batch tasks at scheduling
    time, before any agent process was invoked.

    ``groups`` is a list of dicts, one per colliding pair:
    {"a": int, "b": int, "reason": str, "path_a": str, "path_b": str}.
    """

    def __init__(self, groups: list[dict]):
        super().__init__(self._report(groups))
        self.groups = groups

    @staticmethod
    def _report(groups: list[dict]) -> str:
        lines = ["Output collision in parallel batch — no agent was "
                 "invoked (exit 2)."]
        for g in groups:
            lines.append(f"  tasks {g['a']} and {g['b']}: {g['reason']}")
        return "\n".join(lines)


def _normcase(p: Path) -> Path:
    """Path normalized for case-insensitive comparison where the platform
    FS is case-insensitive. os.path.normcase lowercases on Windows and is
    identity on POSIX (Linux CI unaffected); posixpath is also a no-op on
    macOS, whose case-insensitive APFS volumes stay a documented
    limitation (design F9)."""
    return Path(os.path.normcase(str(p)))


def collides(a: Task, b: Task) -> Optional[str]:
    """R1.2 collision predicate for one task pair. Returns the reason
    string when a and b may corrupt each other's deliverables if run
    concurrently, else None. Fully resolved paths (output_path() already
    resolves against the task's cwd; workdir() resolved here).

    Same-cwd pairs (C1 exemption) are checked for identical/nested
    outputs only; containment is checked between distinct resolved cwds,
    in both directions, and applies even when one task is stdout-only
    (C5 — its agent's side effects can land on the other task's
    deliverable inside its working tree)."""
    pa, pb = a.output_path(), b.output_path()
    if pa is None and pb is None:
        return None
    na = _normcase(pa) if pa is not None else None
    nb = _normcase(pb) if pb is not None else None
    if na is not None and nb is not None:
        if na == nb:
            return f"identical output paths: {pa}"
        if na.is_relative_to(nb) or nb.is_relative_to(na):
            return f"nested output paths: {pa} / {pb}"
    ca, cb = (_normcase(Path(a.workdir()).resolve()),
              _normcase(Path(b.workdir()).resolve()))
    if ca == cb:
        return None  # C1: same-cwd batches are the runner's own shape
    if na is not None and na.is_relative_to(cb):
        return f"output inside the other task's cwd: {pa} / {cb}"
    if nb is not None and nb.is_relative_to(ca):
        return f"output inside the other task's cwd: {pb} / {ca}"
    return None


def _path_label(task: Task) -> str:
    """Resolved output path for reports; stdout-only tasks are labeled."""
    p = task.output_path()
    return str(p) if p is not None else "(stdout-only)"


def _pair_collisions(tasks: list, indices: list) -> list[dict]:
    """All colliding pairs among the given task indices (O(n^2);
    batches are small — docstring contract)."""
    groups = []
    for k in range(len(indices)):
        for m in range(k + 1, len(indices)):
            i, j = indices[k], indices[m]
            reason = collides(tasks[i], tasks[j])
            if reason:
                groups.append({"a": i, "b": j, "reason": reason,
                               "path_a": _path_label(tasks[i]),
                               "path_b": _path_label(tasks[j])})
    return groups


def detect_collisions(tasks: list) -> list[dict]:
    """All colliding pairs of a batch (plain-path pre-check)."""
    return _pair_collisions(tasks, list(range(len(tasks))))


def resolve_plan(tasks: list, waves: Optional[list]) -> list[list[int]]:
    """Execution waves for run_parallel; validates before any agent.

    waves=None (plain path): full pairwise pre-check, fail closed with
    OutputCollisionError on any collision; returns a single wave with
    all task indices in submission order.

    waves=plan (wave path): contract — every node id is str(i) for
    0 <= i < len(tasks), appears exactly once; each wave's members are
    pairwise collision-free under collides(); cross-wave conflicts are
    allowed by design (separating conflicts into different waves is the
    point of a plan). Any violation — including a plan of the wrong
    shape — raises OutputCollisionError, never TypeError/ValueError.

    Empty task list → [] (no-op, both modes). An empty plan for a
    non-empty batch fails closed (coverage rule; design C4 — a silent
    no-op would report success for a batch that ran nothing).
    """
    if not tasks:
        return []
    if waves is None:
        groups = detect_collisions(tasks)
        if groups:
            raise OutputCollisionError(groups)
        return [list(range(len(tasks)))]
    return _validate_plan(tasks, waves)


def _plan_error(reason: str) -> OutputCollisionError:
    return OutputCollisionError([{"a": -1, "b": -1, "reason": reason}])


def _node_index(nid: object, n_tasks: int, max_digits: int) -> Optional[int]:
    """Canonical node id → task index, or None when invalid (fail closed).

    Valid ids are str(i) for 0 <= i < n_tasks. The digit-count guard
    bounds the string before int(), so absurdly long ids (which would
    trip CPython's int max-str-digits ValueError at 4300 digits) fail
    closed instead of crashing; the int() wrapper is defense-in-depth,
    unreachable given the guards."""
    if not isinstance(nid, str) or not nid.isdigit():
        return None
    if len(nid) > max_digits:
        return None
    try:
        idx = int(nid)
    except (TypeError, ValueError):
        return None
    if idx >= n_tasks or str(idx) != nid:
        return None
    return idx


def _validate_plan(tasks: list, waves: object) -> list[list[int]]:
    """Shape-validate a wave plan and check intra-wave collisions."""
    if not isinstance(waves, list):
        raise _plan_error(f"waves must be a list of waves, got "
                          f"{type(waves).__name__}")
    plan, seen = [], set()
    max_digits = len(str(len(tasks) - 1))
    for wave in waves:
        if not isinstance(wave, list):
            raise _plan_error(f"each wave must be a list of node ids, "
                              f"got {type(wave).__name__}")
        members = []
        for nid in wave:
            idx = _node_index(nid, len(tasks), max_digits)
            if idx is None or idx in seen:
                raise _plan_error(f"invalid or duplicated node id {nid!r} "
                                  f"(must be str(i), 0<=i<{len(tasks)})")
            seen.add(idx)
            members.append(idx)
        groups = _pair_collisions(tasks, members)
        if groups:
            raise OutputCollisionError(groups)
        plan.append(members)
    missing = [i for i in range(len(tasks)) if i not in seen]
    if missing:
        raise _plan_error(f"plan covers {len(seen)}/{len(tasks)} tasks;"
                          f" missing: {missing[:5]}")
    return plan


def tasks_to_nodes(tasks: list) -> list:
    """Task → TaskNode adapter (R3.1). id = str(i) is the documented
    contract that makes plan_waves output directly feedable to
    run_parallel(waves=...). Batch tasks declare no reads or
    dependencies; write_set carries the single resolved output path
    (stdout-only tasks write nothing and never conflict)."""
    return [TaskNode(id=str(i), goal=(t.prompt or "")[:60], argv=[],
                     write_set=[str(t.output_path())]
                     if t.output_path() else [])
            for i, t in enumerate(tasks)]


def plan_waves_for(tasks: list) -> list[list[str]]:
    """plan_waves-style plan for a batch (R3.2) — the first production
    importer of pbatch.waves. Tightens the raw plan under the strict
    R1.2 predicate (C2: plan_waves sees write-set intersection only,
    which cannot express path containment), so the returned plan always
    passes resolve_plan's intra-wave validation. Same blocked-defer
    doctrine as waves.py:60-73; terminates because a single-member wave
    is always valid."""
    nodes = tasks_to_nodes(tasks)
    return list(_tighten(tasks, waves_plan_waves(nodes)))


def _tighten(tasks: list, waves: list) -> list:
    """Re-check each wave under collides() and defer colliding members
    to later waves; residual deferred members are re-run over themselves
    (they may collide with each other) until every emitted wave is
    collision-free."""
    by_id = {str(i): i for i in range(len(tasks))}
    carry = []
    for wave in waves:
        members, carry = carry + list(wave), []
        clean = []
        for nid in members:
            if any(collides(tasks[by_id[nid]], tasks[by_id[o]])
                   for o in clean):
                carry.append(nid)
            else:
                clean.append(nid)
        if clean:
            yield clean
    while carry:
        members, carry = carry, []
        clean = []
        for nid in members:
            if any(collides(tasks[by_id[nid]], tasks[by_id[o]])
                   for o in clean):
                carry.append(nid)
            else:
                clean.append(nid)
        if clean:
            yield clean
```

Line/function budget: all functions ≤50 lines; module ≤1000 — passes
`quality.py --strict` (review round: 267 lines, verified). No import cycle
(leaf: `models`/`fabric_sched`/`waves` only; F2).

### 2.2 `pbatch/runner.py` — unified `run_parallel` (net +1 lines + 1 import → 998)

Replace the body at :935-962 (28 lines) with the unified wave loop (31
lines); add one top-level import. Exact reference implementation:

```python
from .parallel_plan import resolve_plan        # top-level import, near :27

def run_parallel(tasks: list[Task], workers: int = 0, validate_cmd: str = "",
                 retries: int = 0, retry_delay: float = 10.0, backoff: float = 2.0,
                 min_interval: float = 0.0,
                 waves: Optional[list] = None) -> list[TaskResult]:
    """Concurrent execution with per-result validation gates (fail-closed).
    T8: budget cap pre-submit + atomic consume; cap trip exits 3 promptly.
    Failed tasks retry with exponential backoff up to `retries` extra
    attempts (same helpers/defaults as run_serial); `min_interval` throttles
    between successful completions (first undelayed); zeros are no-ops.
    Colliding outputs fail closed; `waves` adds barriers (parallel_plan)."""
    workers = workers or config.AGENT_DEFAULT_WORKERS
    waves = resolve_plan(tasks, waves)  # raises OutputCollisionError
    log.info("PARALLEL x%d  (%d tasks)", workers, len(tasks))
    results, capped = [], False
    for wave in waves:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fut_map = {}
            for i in wave:
                if not budget_allows():
                    capped = True
                    log.error("BUDGET: invocation limit reached; stopping — 调整 "
                              "--max-rounds/limits 后可续跑（已保留现场）")
                    break
                fut_map[pool.submit(_run_parallel_one, tasks[i], i + 1, len(tasks),
                                    validate_cmd, retries, retry_delay, backoff)] = tasks[i]
            wave_results, capped = _collect_parallel(fut_map, len(wave), min_interval, capped)
            results.extend(wave_results)
        if capped: raise SystemExit(3)  # 折叠：保 1000 行门禁余量
    return results
```

Accounting: the reference body above is 29 lines vs the old 28
(signature +1, docstring +1, `resolve_plan` call +1, wave-loop overhead
+3, dropped `total` var −1, dropped `# Pass task_index` comment −1,
folded `results, capped` init −1, folded `if capped: raise` −1) → +1;
plus the top-level import (+1) → **996 + 2 = 998 lines**, 2 lines of
headroom below the 1000-line gate (review hardening: the design's
unfolded variant would land at 999/1000 — the fold is now the reference).
The `total` local is removed (`len(tasks)` inlined for `_run_parallel_one`
numbering; `len(wave)` is passed to `_collect_parallel` so the PROGRESS
log is per-wave monotonic — plain path identical because its single wave
is all tasks). `min_interval` throttles per wave (per-wave
`_collect_parallel` counter — documented contract, plain path
unchanged). Plain path is byte-for-byte equivalent behavior: single wave
= all indices ascending, one `ThreadPoolExecutor`, one `_collect_parallel`
drain, budget-cap `SystemExit(3)` after the pool, `kill_active_procs`
inside `_collect_parallel` (F3). Wave mode: barrier per wave; a cap
mid-wave raises before the next wave starts.

### 2.3 `pbatch/cli_batch.py` — catch + exit 2 (around :496)

```python
    else:
        try:
            results = run_parallel(tasks, args.workers, validate_cmd=cli_validate,
                                   retries=args.retries, retry_delay=args.retry_delay,
                                   backoff=args.retry_backoff, min_interval=args.min_interval)
        except OutputCollisionError as exc:
            log.error("%s", exc)          # stderr via basicConfig (F5)
            sys.exit(2)
```

Plus one import: `from .parallel_plan import OutputCollisionError` (next to
the `.runner` import, with `sys` already imported at cli_batch.py:20). The
catch sits in `_execute_batch` — deliberately **below** `_run_batch_round`'s
round loop so a collision is a hard batch-definition error (exit 2, house
pattern), not a retryable round failure that the 7×24 loop would spin on
forever. `SystemExit(3)` (budget cap, mid-execution) is deliberately NOT
caught here — it keeps its pre-existing exit-3 propagation; only the
scheduling-time `OutputCollisionError` maps to exit 2.

### 2.4 Documentation

- `AGENTS.md` §2.2 (result validation) gains one bullet: parallel batches
  with colliding output paths are rejected at scheduling time (exit code 2,
  no agent invoked); `run_parallel(waves=...)` co-schedules non-conflicting
  tasks with a per-wave barrier; **wave plans may contain cross-wave
  conflicts by design** (later waves serialize — that is the point of a
  plan) **and the check is a scheduling-time heuristic, not a write-safety
  sandbox** (same-cwd batches are the runner's standard shape; agents'
  shared-directory side effects are the accepted baseline). Proposed bullet:

  > - 并行批次输出路径冲突（identical / nested / 输出落在对方不同 cwd 内，
  >   含 stdout-only 任务的 cwd 参与 containment）在调度期 fail closed：
  >   `OutputCollisionError` → stderr 报告 → exit 2，任何 agent 不启动；
  >   `run_parallel(waves=...)` 可显式分波（每波屏障、波内无冲突；波间
  >   冲突按设计允许——后续波串行化正是分波的意义）。这是调度期启发式，
  >   不是写安全沙箱：同 cwd 批次是 runner 的标准形态，agent 共享目录
  >   副作用是既成基线（C1/C5）。
- Contracts documented in `parallel_plan.py` module/function docstrings;
  `run_parallel` docstring points to them (C3).

No changes: `pi-batch.yaml`, `waves.py`, `fabric_sched.py`, `models.py`,
`pipeline.py`, `campaign_analysis.py`, `pbatch/__init__.py`, CLI flags.

## 3. Compatibility constraints

| Constraint | Detail |
|---|---|
| `run_parallel` signature | Additive: `waves: Optional[list] = None` — all 3 callers unchanged; `waves=None` behaves as today for collision-free batches (single wave, submission order, same helpers) |
| Behavior change (intended) | Colliding parallel batches now fail before any agent: single-batch exit 2; pipeline/campaign raise → loud failure, zero artifacts |
| Same-cwd batches | **Not** flagged (C1) — campaign analysis, `from_dir` stages, default-cwd tasks.yaml all keep working; regression test added |
| `waves` plans | Relative to the exact `tasks` list passed to `run_parallel` (post-`--reuse` filtering, F4); API-only today, no CLI/YAML wiring (spec non-goal) |
| Serial mode | Untouched — same-output serial tasks keep deterministic last-writer-wins (pre-existing semantic, out of scope) |
| Public API | `pbatch/__init__.py` untouched; `OutputCollisionError` importable from `pbatch.parallel_plan` |
| Python floor | `>=3.9` (`Path.is_relative_to`, `Path.resolve`) |
| Line budget | runner.py 998 ≤ 1000 (`make ci` quality gate, C3 + review fold); new module passes `quality.py --strict` |
| Existing tests | All collision-free under the corrected predicate (F6); parallel retry/min-interval/validator-feedback tests stay green |
| Waves internals | `waves.py`/`fabric_sched.py`/`TaskNode` byte-identical; their 5 test files untouched |

## 4. Failure modes

| # | Mode | Behavior | Design intent |
|---|---|---|---|
| F1 | Plain batch with colliding outputs (identical / nested / output-in-other-distinct-cwd) | `OutputCollisionError` → cli_batch prints report → exit 2; no agent invoked, no files written | Fail-closed doctrine; T1 |
| F2 | Invalid `waves` plan — **any shape**: int/str/tuple plan, non-list wave, non-str id, duplicate/missing/out-of-range id, leading-zero or unicode-digit id, id too long to be canonical (incl. >4300 digits) | Same `OutputCollisionError`, exit 2 before any agent; never TypeError/ValueError | R2.3 + C4/C6; T2 negative |
| F3 | Budget cap trips mid-wave | `SystemExit(3)` raised before the next wave; in-flight agents killed via `_collect_parallel`'s `kill_active_procs` | R2.4 preserved; existing T8 semantics |
| F4 | Collision inside a pipeline stage or campaign analysis | Exception propagates → loud failure (traceback + exit 1), zero corruption; report string is self-contained for diagnosis | R1.5 per spec (no pipeline/campaign signature changes); alternative (clean stage failure) noted in §7 |
| F5 | `--reuse` masks a collision | Reused+validated task is dropped before the check; remaining set executes | Acceptable: the artifact exists and was re-validated; no race remains. Documented in AGENTS.md bullet |
| F6 | `--dry-run` on a colliding batch | Prints tasks, exit 0; the real run then exits 2 | Known limitation; optional follow-up (§7) |
| F7 | Symlink aliasing of outputs | `output_path().resolve()` follows symlinks → aliased targets compare equal → flagged; symlinked cwd spellings resolving to one dir are ONE cwd → C1 exemption applies correctly (no false positive) | Better than today (silent corruption); pinned by T1e |
| F11 | Stdout-only task whose (distinct, nested) cwd contains another task's deliverable | Containment applies even when one output is None (C5) — flagged before any agent | C5; T1f |
| F8 | Truncated plan (`plan_waves(..., max_wave=...)`) fed to `run_parallel` | Coverage violation → fail closed | Contract: full coverage only |
| F9 | Case-insensitive filesystems | Windows: `os.path.normcase` lowercases → case-variant paths flagged; Linux CI: identity → byte-identical behavior; macOS APFS case-insensitive volumes remain undetected (`posixpath.normcase` is a no-op on darwin) | Documented; simulated-Windows test pins the normcase path |
| F10 | O(n²) pre-check on large batches | n = batch size (typically ≤ dozens); documented in docstring | R1.7 — no optimization needed |

## 5. Migration steps

1. **Phase 1 — add `pbatch/parallel_plan.py`** (pure addition). Verify:
   `python -c "import pbatch.parallel_plan"`, `python quality.py --strict .`
   unchanged, `make ci` green. No behavior change anywhere.
2. **Phase 2 — rework `run_parallel`** (unified wave loop, §2.2) + add
   `cli_batch` catch (§2.3, +5 lines net). Existing parallel tests stay green (F6);
   new fail-closed behavior is now live. Spot-check a colliding tasks.yaml
   manually: `python pi-batch.py tasks.yaml --mode parallel` → exit 2.
3. **Phase 3 — tests** (§6): T1 (main + T1b/T1c/T1d/T1e/T1f + same-cwd
   regression + CLI companion), T2 (+ negatives + bad shapes + wave-cap
   pin), T3 in `test_runtime_governance.py`. The review-round harness
   (`/tmp/pbatch-review`, 41 cases over a copy of the patched package:
   F2 shapes, C5 containment, symlink/case edges, wave barrier, budget
   cap, exit-2 companion) is the starting point for these tests.
   `make ci` + `python quality.py --strict .` (runner ≤1000 — 998,
   new module clean).
4. **Phase 4 — docs**: AGENTS.md §2.2 bullet (§2.4). Review diff: `git diff
   --stat` must show only `parallel_plan.py` (+new), `runner.py` (+2/-0 net),
   `cli_batch.py` (+5), 2 test files, `AGENTS.md`.
5. **Rollback**: revert phases 2-4 only; `parallel_plan.py` is inert without
   its runner call site. No config/schema/migration impact (no persisted
   state, no YAML, no CLI flags).

## 6. Testable acceptance mapping

| Requirement | Test (file) | Assertion |
|---|---|---|
| R1.1 pre-check before pool | `test_pi_batch.py::test_parallel_output_collision_fails_closed_no_agent` | `OutputCollisionError` raised; counter file absent (no agent invoked); no output file created |
| R1.2a identical outputs (relative vs absolute) | same, main case | Two tasks, `output="o.md"` + `output=str(tmp_path/"o.md")`, same cwd → reason "identical output paths"; report names both indices + resolved path |
| R1.2b containment (C1+C5) | sub-case T1b | Nested distinct cwds (A cwd=tmp, output=tmp/o-a.md; B cwd=tmp/b, output=tmp/b/a.md) → collision; **C5**: same shape with A or B stdout-only → still collision; output == other cwd exactly (A output=tmp/b, B cwd=tmp/b, output outside it) → collision; **regression**: same cwd, distinct outputs (campaign/from_dir shape) → no collision, both succeed; same basename in distinct dirs → no collision |
| R1.2c nested outputs | sub-case T1c | A output=`tmp/dir`, B output=`tmp/dir/x.md` → collision ("nested output paths") |
| R1.3 stdout-only exempt | sub-case T1d + T1f | Two `output=""` tasks + one distinct-output task, same cwd → no collision, all `success=True`; **T1f**: stdout-only task in a distinct nested cwd containing another's deliverable → collision (C5) |
| R1.2d symlink aliasing + case | sub-case T1e | `tmp/link → tmp/real`: outputs `tmp/real/o.md` vs `tmp/link/o.md` → identical; cwds `tmp/real` vs `tmp/link` (distinct spellings, one dir) + distinct outputs → **no** collision; Linux `Foo.md`/`foo.md` → no flag (normcase identity); simulated Windows normcase → flagged |
| R1.4 exception report | T1 | `exc.groups` contains the pair; `str(exc)` names indices and paths |
| R1.5 exit 2, no agent | `test_cli_parallel_collision_exit_2` (subprocess, tasks.yaml, `--mode parallel`) | returncode 2; stderr names both tasks + paths (F5); counter agent never invoked |
| R2.1 `waves` param | `test_parallel_wave_plan_co_schedules_non_conflicting` | t0→out-a, t1→out-b, t2→out-a; `plan_waves_for` → `[["0","1"],["2"]]`; `run_parallel(tasks, workers=2, waves=plan)` succeeds |
| R2.2 per-wave barrier | same | Overlap log (agent timestamps): t0/t1 overlap in wall-clock; t2 starts only after t0 ends; final out-a content == t2's write (deterministic, no concurrent writers) |
| R2.3 plan validation | `test_parallel_wave_plan_invalid_fails_closed` + T2b | t0+t2 in one wave → `OutputCollisionError` before any agent; missing index; duplicated index — same; **shape (C6)**: `waves=5`, `waves="01"`, tuple plan, non-list wave, int ids, leading-zero/unicode-digit ids, `"9"*5000` → all `OutputCollisionError`, never TypeError/ValueError |
| R2.4 semantics preserved | existing parallel tests (retries :425-460, min_interval :486-508, validator feedback :460-485) stay green + T2c | wave mode reuses `_run_parallel_one`/`_collect_parallel`; budget-cap behavior covered by existing T8 tests + F3; **T2c pin (qa_lead F3)**: budget=1 + 2-wave plan → `SystemExit(3)`, second wave never starts |
| R2.5 edges | T2 negative + T1 | `run_parallel([])` → `[]`; `run_parallel([], waves=[])` → `[]`; bad id (`"x"`, `"01"`, `"9"`, `"9"*5000` for n=3) → fail closed; empty plan for non-empty batch → fail closed (C4) |
| R3.1 adapter contract | `test_runtime_governance.py::test_plan_waves_fed_to_run_parallel_preserves_deliverables` | Tasks a→file-x, b→file-x, c→file-c; TaskNodes with `id=str(i)`, `write_set`=resolved paths; raw `plan_waves` → `[["0","2"],["1"]]` fed to `run_parallel`; file-c intact, file-x == b's write, overlap log: a+c concurrent, b after a (no concurrent writers of file-x) |
| R3.2 first production importer | `grep` + T2/T3 | `parallel_plan.py` imports `pbatch.waves`; `plan_waves_for` output accepted by `resolve_plan` (guaranteed by C2 tightening) |
| R3.3 waves.py untouched | `git diff --stat` | `waves.py`, `fabric_sched.py` not modified; 5 existing waves test files untouched |
| R4 documentation | review | AGENTS.md §2.2 bullet; `resolve_plan` docstring contains the full contract; `run_parallel` docstring points to it |
| Engineering gates | `make ci` + `python quality.py --strict .` | 387 regression tests + 25 eval cases + harness green; runner.py 998 ≤ 1000; new module clean (functions ≤50, file ≤1000) |

## 7. Out of scope / follow-up recommendations

- **Serial-mode same-output overwrite**: pre-existing deterministic semantic;
  explicitly out of scope (R1 targets the parallel path only).
- **CLI/YAML wiring for waves**: no flags, no `pi-batch.yaml` keys (spec
  non-goal); `plan_waves_for` is ready for a future caller (e.g., a
  `--parallel-waves` flag that auto-plans instead of failing — the natural
  follow-up direction).
- **Pipeline clean-stage-failure**: spec chose "let propagate"; a later
  nicety would catch `OutputCollisionError` in `_execute_stage_tasks` and
  record a decision-log entry — not needed for correctness.
- **`--dry-run` collision check** (F6): 3-line addition
  (`parallel_plan.detect_collisions` in `_print_dry_run`); strictly better
  UX ("dry-run shows what would fail"), deferred to keep the change surface
  at spec.
- **R2.5 empty-plan** interpretation (C4) and **R1.2b distinct-cwd scoping**
  (C1) are deliberate deviations — flag both in the review notes.
