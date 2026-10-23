# Campaign live execution observations

Campaign directions now update the existing task-center snapshot while other
isolated directions are still executing. This is local runtime observation, not
new execution authorization, a progress percentage, or goal acceptance.

## Lifecycle and ownership

After selection/admission, the coordinator appends `QUEUED` for every direction.
Preparing a worktree or submitting a future does **not** mean `RUNNING`.

For isolated execution, the coordinator still prepares the worktrees first,
then keeps at most the effective `jobs` futures in flight. Each worker checks
the original frozen campaign deadline before entering its execution attempt and
sends one small start signal. The coordinator drains these signals and records
`RUNNING`; workers never call observers or mutate `StateStore`'s cache.

The start signal queue holds at most `jobs` entries. The pump checks it every
50 ms while waiting for completion, and before handling completed futures.
Coordinator filesystem/observer work and OS scheduling can delay this interval;
it is not a hard real-time promise. A worker that finishes before its start
signal is consumed still has its start recorded before its completion.

On return from a worker, the coordinator immediately appends
`EXECUTION_COMPLETED`, carrying the explicit `execution_status`, evidence,
recovery facts and scheduling measurements. This fact is visible in the same
`ops tasks` / `/api/tasks` snapshot while slower siblings remain blocked.
It does not mean `MERGED`, accepted, released, or that a provider request was
cancelled. A refused admission or preparation failure can complete without ever
having been `RUNNING`; its execution timings remain unknown.

Non-passing outcomes are terminally recorded immediately through the existing
`record_campaign_outcomes` path. They are not recorded again at batch end.
Passing candidates retain `EXECUTION_COMPLETED` until final batch handling.
That final handling still uses `record_campaign_outcomes` and the existing
`--auto-merge` opt-in, review/merge controller, repository lock and gates.
A merge refusal remains `MERGE_BLOCKED`, even if `scheduling.execution_status`
says `PASSED`. No progress observer can authorize or bypass a merge.

Prior failure identity/counts survive `QUEUED`, `RUNNING` and the completion
fact. The existing failure-accounting implementation runs once per outcome;
completion and terminal observations share an attempt identity, so task-center
failure counts and reported work duration do not double-count them.

## Timing definitions

Optional `task.scheduling` has a fixed, bounded shape:

```json
{
  "queue_wait_seconds": 2.5,
  "execution_wall_seconds": 14.0,
  "execution_status": "PASSED"
}
```

- `queue_wait_seconds`: monotonic interval from admission to this implementation
  batch's queue until actual execution-attempt entry. It includes coordinator
  worktree preparation and waiting for a dispatch slot. Before entry it is
  `null`, not an estimated/growing wait value.
- `execution_wall_seconds`: monotonic interval from execution-attempt entry to
  worker return, measured before completion-observer I/O and deferred merging.
  It includes preparation inside the attempt, pipeline execution, supervision,
  evidence collection and applicable recovery. It is `null` until observed.
- `execution_status`: the observed execution result, not the merge result or an
  acceptance decision. It is `null` before completion.

For serial execution, the coordinator publishes start at the attempt-call
boundary, executes the existing safe serial path, and measures its return.
That interval includes synchronous start-observation overhead. Serial execution
remains serial and shares the same frozen deadline and supervision as before.

These are direction-level wall intervals, **not** LLM thinking/tool durations,
CPU time, provider latency, heartbeat/liveness, utilization, cost, ETA, or a
remaining DAG. Measured zero is valid; unstarted/untracked/invalid intervals are
`null`. Projection accepts only finite, nonnegative numeric timings up to the
existing numeric observation bound (`1e15`); booleans and numeric strings are
not measurements. Unknown old records have `task.scheduling: null`. No old
telemetry is backfilled and no unrelated cost/acceptance fields are zero-filled.

## Persistence and failure boundaries

Scheduling data lives on the existing campaign ledger events (including final
outcomes), not in a new progress API or a per-tick metrics log. Default state,
worktrees, deadline receipts and failure evidence stay in the project's private
runtime outside source Git; explicit existing path overrides retain their
meaning. The existing `StateStore` append/fsync and per-line limit (normally
256 KiB) apply. Each admitted direction adds at most four lifecycle/outcome
rows per batch; there is no periodic persistent timing stream. Existing ledger
retention semantics are unchanged, so total history is still append-only.
Task-center reads retain their existing bounded source/window/reference limits
and scheduling projection copies only the three fields above.

All required ledger/observer failures propagate. Queue/start/completion writes
are not best-effort telemetry: failure stops fresh dispatch; unstarted futures
are cancelled, and already running attempts finish under their existing
ownership, timeout/watchdog and deadline guards. The pump does not invent a
new cancellation authority, kill unrelated processes, roll back effects or
replay unknown work. Waiting for these attempts can delay error propagation.
Existing preservation/receipt failures and gate refusals remain fail-closed.

## Compatibility and limits

`run_directions_isolated(root, settings, items, jobs, agent_args)` remains valid
and returns a list of the same outcome objects (with additive optional
`scheduling`). Keyword-only hooks are optional:

- `on_start(direction, scheduling)` runs on the caller/coordinator thread after
  consuming a worker start signal;
- `on_outcome(outcome)` runs on that same thread for each outcome, including
  admission/preparation refusals, before batch completion;
- `queued_at` optionally carries the coordinator's monotonic batch-queue origin.

Without hooks the isolated path does not create campaign lifecycle ledger rows.
Completion ordering among simultaneously ready workers is unspecified. Serial
`run_direction` retains its original call shape and return behavior.

Worktree preparation remains batched, so a slow preparation can delay every
worker's start. Successful candidates still wait for slower siblings before
final publication/authorized merge. A stopped coordinator can therefore leave
an execution-completed candidate without a final merge result; that marker is
not reusable `PASSED` evidence. This change does not add restart/replay policy,
live merging, a seven-day campaign guarantee or multi-device authorization.

## Verification scope

`tests/test_campaign_live_dispatch.py` uses owned event-controlled workers and
real temporary campaign ledgers/task snapshots to cover queue/start distinction,
bounded submissions/refill, live completion, coordinator ownership, required
write failure, original deadline refusal, legacy calls, merge refusal and
single failure accounting. Existing budget and real-child deadline tests remain
in the targeted gate. These offline fixtures do not constitute provider,
production, actual-phone or seven-day acceptance.
