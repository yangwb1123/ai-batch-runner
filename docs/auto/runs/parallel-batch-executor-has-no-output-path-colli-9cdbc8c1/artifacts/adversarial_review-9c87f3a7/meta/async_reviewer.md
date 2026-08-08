All six audit points verified against the live tree (`runner.py:886–962`, `metering.py:233–260`, `waves.py`, the reference loop in the design §2.2) plus one empirical probe of the `SystemExit`/executor interaction. Review follows.

---

# Review — Unified wave loop concurrency semantics (`pbatch-ba9cd1a4`)

**Method**: read the reference loop (§2.2), `_run_parallel_one` (:886), `_collect_parallel` (:909), `_budget_gate` (:106), `budget_allows/try_consume` (metering.py:233/244), `_kill_group` (:138), `_retry_wait` (:367), `ready_nodes` (fabric_sched.py:70), requirements R2.4/R2.5 text; ran an empirical probe of `SystemExit(3)` + `ThreadPoolExecutor` join behavior (results below).

## 1. Barrier correctness between waves — **sound**, exact mechanics pinned

The reference loop's barrier is a **drain barrier, double-enforced**:

```python
for wave in waves:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        ...submit all wave members...
        wave_results, capped = _collect_parallel(fut_map, len(tasks), min_interval, capped)
        results.extend(wave_results)
    if capped: raise SystemExit(3)
```

- `_collect_parallel` iterates `as_completed(fut_map)` **to exhaustion** — no early break on failure, cap, or None marker. Queued tasks (beyond `workers`), retries, and backoff sleeps are all included before the wave completes.
- The `with` exit runs `shutdown(wait=True)` after the drain — a second enforcement that also covers the exception path (see §2).
- **Failures do not block later waves**: a permanently-failed task is appended as a failed `TaskResult` and the loop proceeds. The barrier is a *completion* barrier, not a *success* gate — correct here because waves separate write conflicts only, never data dependencies (no reads in `tasks_to_nodes`).
- Subtlety worth pinning: if a worker raises an *unexpected* exception, `fut.result()` propagates in the main thread mid-drain → batch aborts, remaining wave futures' results are discarded — but the `with`-exit `shutdown(wait=True)` still joins them. Pre-existing plain-path property; unchanged.

## 2. Budget cap mid-wave: in-flight workers are **drained and joined — never orphaned, never killed**. (headline answer)

There are two trip paths, both funnel into the same exit:

1. **Pre-submit**: `budget_allows()` False during a wave's submission loop → `break`, remaining wave members never submitted → `_collect_parallel` drains the partial `fut_map` → `SystemExit(3)`.
2. **In-worker** (authoritative): `budget_try_consume()` (atomic, metering.py:244) fails in `_budget_gate` → `SystemExit(3)` inside the worker → `_run_parallel_one` catches code 3 → **None marker** → `_collect_parallel` sets `capped`, calls `kill_active_procs()` (kills agent *process groups*, `_kill_group` = group SIGKILL + reap), **and keeps draining** the remaining futures.

Then `if capped: raise SystemExit(3)` runs **after** the drain and **after** the `with`-exit `shutdown(wait=True)`. So at the moment `SystemExit(3)` leaves `run_parallel`, zero worker threads are in flight. Empirical probe (CPython 3.10, same mechanics as the reference loop):

```
P1 SystemExit raised while 0.8s worker in flight → escaped at t=0.801s, worker 'end' logged
   → with-block shutdown(wait=True) blocked until the worker finished (JOINED)
P2 SystemExit after full drain → escaped at t=0.151s (no wait)
P3 executor threads are non-daemon → even a bare executor (no with) would be joined at interpreter exit
```

Three contract-relevant caveats, all honest:

- **Exit-3 latency is bounded, not instant**: a worker mid-retry-backoff (`time.sleep` in `_run_parallel_one`, 30–60 s for rate/network class per `_retry_wait`) cannot be interrupted by `kill_active_procs` (that kills subprocesses only). It wakes, hits `_budget_gate`, returns None. Bound = slowest in-flight worker's (post-kill agent time + ≤1 retry-wait). Not the 900 s agent timeout — agents are killed.
- **Capped-wave deliverables stay on disk**: in-flight agents that completed before/despite the kill wrote their outputs (`mkstemp`+rename), but the wave's results are discarded at exit 3. Pre-existing plain-path semantics (`if capped: raise` discards too), but wave mode should pin it — a 7×24 operator inspecting `.pi-batch/` will see partial artifacts from the aborted wave.
- **Pre-submit `budget_allows()` is a fast-path early stop, not a reservation**: consume happens atomically in the worker. Wave k+1's submissions may pass the pre-check while wave-k workers still hold unconsumed budget → cap trips *inside* wave k+1 → exit 3 with that wave partially run. Correct (cap is eventual, exit 3 + kill), but the contract should say so.

## 3. Retry behavior inside a wave — **preserved, no cross-wave leak**

Retries live inside the worker thread (`_run_parallel_one`, `range(retries + 1)`):

- **Contained within their wave**: the barrier waits for retry exhaustion (backoff sleeps included) before wave k+1 is submitted. No retry can straddle a barrier.
- **Cap never retried**: `SystemExit(3)` from `_budget_gate` → None marker ("never retried past", :889). A cap during a retry attempt stops that task's retries; the *wave* still drains, then the batch exits 3 (cap wins over everything — T8 doctrine preserved).
- Validator feedback (`_retry_task_with_feedback`) is worker-local; no cross-task interference.
- One inherent property to document: a retrying task holds a pool slot, so its backoff delays *queued same-wave* tasks (shared pool — identical to plain path; the barrier absorbs it).

## 4. F4 decision — `min_interval`: **CUMULATIVE (batch-global), contract pinned**

**Confirmed**: `_collect_parallel` initializes `successes = 0` per call; the unified loop calls it once per wave → the boundary between wave k's last success and wave k+1's first success gets **no throttle**. With instant agents + fast drain, two successes can land back-to-back across a barrier despite `min_interval=5`.

**Decision: cumulative.** Rationale:

1. **R2.4's literal text**: "`min_interval` throttling between successful completions [is preserved] across waves, unchanged". The plain path throttles *every* success pair batch-wide; the wave path must not weaken it.
2. **Purpose**: `run_serial`'s docstring — "successful tasks are throttled … so long-running 24×7 batches do not hammer the provider". Provider load doesn't know about wave boundaries; a 2-success burst at a barrier is exactly the hammering the knob exists to prevent.
3. **Coherence**: per-wave semantics makes the throttle *plan-dependent* — splitting a batch into more waves (or auto-planning later, the design's own follow-up) silently reduces throttling for identical work. A provider-protection knob must be invariant to plan shape.

Implementation is two tokens: `_collect_parallel(fut_map, total, min_interval, capped=False, successes_offset=0)` and `successes = successes_offset` (replaces `successes = 0`); the loop passes the accumulated count. Plain path passes the default → existing T3a/T3b pins (`sleeps == [5.0]` / `[5.0, 5.0]`) stay green. Cost: +1 signature line → fold `if capped: raise SystemExit(3)` (architect L2) to stay at 998.

Rejected alternative (tracked for the record): sleep `max(0, min_interval − actual_gap)`. It changes within-wave behavior for slow agents, breaks the documented "k successes → k−1 sleeps of `min_interval`" rule and its exact-sleep pins. Keep the existing fixed-sleep rule; extend it cumulatively.

## 5. PROGRESS counter regression — **confirmed, pinned monotonic-cumulative**

The reference passes `len(tasks)` (global) as `total` but logs per-wave `len(results)`: plan `[["0","1"],["2"]]` with n=3 emits `PROGRESS: 1/3`, `2/3` … then `PROGRESS: 1/3` — **backward**. A log monitor grepping the completion sentinel (`PROGRESS: N/N done`) never fires; per-wave totals (`2/2`, `1/1`) would break that sentinel too.

**Pinned fix**: `_collect_parallel(..., done_offset=0)`; log `done_offset + len(results)`; the loop passes `len(results)`. Sequence becomes `1/3, 2/3, 3/3` — monotonic, sentinel preserved. No test today pins the format (verified: zero `PROGRESS` assertions in `tests/`), so the pin is net-new and safe.

## 6. Wave ordering determinism — **deterministic at plan granularity**, within-wave completion order is (as today) not

| Aspect | Determinism | Verified via |
|---|---|---|
| Wave order | **Deterministic** = plan order; `resolve_plan`/`_validate_plan` never reorder | design §2.1 |
| Within-wave submission order | **Deterministic** = member order; first `workers` tasks start in submission order (FIFO internal queue) | reference loop `for i in wave` |
| Result list | Wave-grouped in plan order; **completion order inside each wave** (pre-existing plain-path property — `print_summary` already lists completion order today) | `results.extend(wave_results)` |
| `plan_waves` / `plan_waves_for` | **Deterministic** given task-list order: `ready_nodes` is a list-order filter (fabric_sched.py:72), greedy first-fit in ready order, `_tighten` order-preserving | waves.py:56–80, design §2.1 |
| PROGRESS line order, min_interval sleep placement, within-wave wall-clock starts | **Nondeterministic** (completion order) | `as_completed` |

Test hygiene pin: barrier/ordering tests must assert only deterministic things — wave-k+1 start-after-wave-k-complete via agent timestamps (fake agents need ≥100 ms sleeps), final deliverable contents, monotonic PROGRESS counts. Never assert within-wave line/start order. Plan-order pin (shuffled valid plan `[[2],[0,1]]` executes in plan order, qa_lead L3) is deterministic and worth adding.

## 7. Findings

| # | Sev | Finding | Disposition |
|---|---|---|---|
| 1 | **High** | F4: min_interval boundary hole — per-call `successes` reset lets back-to-back successes cross the barrier, contradicting R2.4's "across waves, unchanged" and the knob's 24×7 purpose | **Decided: cumulative** (§4); +1 line, 2 tokens; fold keeps 998 |
| 2 | **Medium** | PROGRESS goes backward across waves (2/3 → 1/3); completion sentinel never emitted in wave mode | **Decided: monotonic cumulative** (§5); `done_offset` param |
| 3 | **Medium** | F3 gap confirmed: wave-mode budget cap ("exit 3, no next wave") has zero test coverage (only plain-path T8/serial tests exist) | Add the pin test (§8, T-cap) |
| 4 | **Info** | Headline question answered: in-flight workers are **joined**, never orphaned — with-block `shutdown(wait=True)` on every exit path, empirically demonstrated; exit-3 latency bounded by kill + ≤1 retry-wait; capped-wave deliverables remain on disk (results discarded) | Pin in contract (§8, items 2) |
| 5 | **Info** | Pre-submit `budget_allows()` is fast-path, not reservation; cap can trip mid-later-wave | Pin in contract |
| 6 | **Info** | Per-wave `ThreadPoolExecutor` = thread churn for many-small-wave plans (plans are typically few waves; acceptable) | Document, no change |
| 7 | **Info** | Ordering determinism verified; within-wave completion order nondeterministic (pre-existing) | Pin in contract; test hygiene (§6) |
| 8 | **Info** | Line budget: §4+§5 additions cost +2 signature lines → adopt architect L2 fold (998) | Include in Phase 2 |

No new correctness defects beyond qa_lead's F4 (now decided). Endorsed as-is: architect's Option B + fold, qa_lead's F1/F2/F3/F5 test additions, F4 as decided here.

## 8. Pinned contract (paste-ready)

Add to `resolve_plan`'s docstring (full contract) + `run_parallel` pointer sentence + `_collect_parallel` docstring (items 4–5):

```
Wave execution contract (unified run_parallel loop):
1. Barrier — a wave is a submission+drain barrier: all wave-k tasks are
   submitted before any result is collected, and wave k+1 is not entered
   until ALL wave-k futures complete (queued tasks, retries, backoff
   sleeps, and failures included). Completion barrier, not success gate:
   failed tasks never block later waves.
2. Budget cap — authoritative at atomic consume inside the worker (T8);
   the pre-submit budget_allows() check is a fast-path early stop, not a
   reservation. Cap trips mid-wave → remaining wave members are not
   submitted, in-flight agents are killed (kill_active_procs), the wave's
   futures are fully drained, then SystemExit(3) is raised AFTER the
   drain and AFTER executor shutdown(wait=True) — in-flight worker
   threads are joined, never orphaned, on every exit path including
   unexpected worker exceptions. Exit-3 latency is bounded by the
   slowest in-flight worker (kill time + at most one retry backoff
   sleep). Deliverables completed by the capped wave remain on disk;
   their results are discarded (pre-existing plain-path semantics). The
   next wave never starts.
3. Retries — per-task, inside the worker thread, contained within their
   wave; the barrier waits for retry exhaustion. A budget-cap
   SystemExit(3) during a retry returns the None marker and is never
   retried. Failed tasks never restart or reorder other tasks.
4. min_interval — CUMULATIVE across the whole batch including wave
   boundaries: successes counted in drain order with no per-wave reset;
   first batch success undelayed, every subsequent success (crossing
   barriers included) preceded by a min_interval sleep (k successes →
   k−1 sleeps). The barrier provides no throttle.
5. PROGRESS — done counter is cumulative and monotonic across waves;
   the final success line is always "PROGRESS: N/N done". Within a
   wave, log-line order and sleep placement follow completion order
   (nondeterministic); only counts and monotonicity are guaranteed.
6. Ordering — wave order = plan order (never reordered); within a wave,
   submission order = member order, first `workers` tasks start in
   submission order; the result list is wave-grouped in plan order with
   completion order inside each wave. plan_waves/plan_waves_for are
   deterministic given task-list order.
```

**Phase-3 test additions** (alongside qa_lead F1/F2/F5):
- `test_parallel_wave_budget_cap_stops_next_wave` (F3): plan `[["0","1"],["2"]]`, budget=1 (monkeypatch consume) → `SystemExit` code 3; task-2 counter absent; wave-1 outputs intact; recording `ThreadPoolExecutor` subclass asserts `shutdown(wait=True)` was called before `SystemExit` escaped (pins the join-before-exit ordering).
- `test_parallel_wave_min_interval_across_barrier` (F4): 2 waves × 1 instant success, `min_interval=5` → `sleeps == [5.0]` (cumulative; `== []` would pin the rejected per-wave semantics).
- `test_parallel_wave_progress_monotonic`: caplog; wave `[["0","1"],["2"]]` → PROGRESS done-counts strictly increasing, final line `3/3`.
- `test_parallel_wave_plan_order_governs`: shuffled valid plan `[[2],[0,1]]` → task-2 start-time precedes 0/1 (plan order, not submission order).

All four fit the existing test-file conventions; no prod surface beyond the two `_collect_parallel` parameters (+1 line, fold → 998/1000).
