# Campaign analysis-phase budget — third implementation batch

This extends the second batch's owned-child cutoff to the coordinator-side
analysis phase. It does not start/resume a campaign, extend a budget, merge
candidates, or certify ForgeOS. Earlier frozen receipts remain separate
identities.

## One shared clock

`run_campaign` freezes a single coordinator cutoff after budget admission and
keeps it on the context (`context.deadline_settings`); analysis, queued
isolated directions and later rounds reuse that exact epoch/monotonic cap and
one-way exhaustion latch. The freeze is a private `replace()` copy with a
runtime-only field, so the original settings object and every settings-derived
fingerprint stay unchanged. Worktree ledgers never grant a new window.

## Analysis admission and bounds

Before any module is prepared, `_analyze_modules` checks the shared deadline.
An expired module is recorded as an honest `BUDGET_BLOCKED` analysis event
(with no `failure_fingerprint`, so a later run with a fresh budget still
re-analyses it) and no model call is dispatched. Building a task that races
past the cutoff is also marked stopped and refused by the dispatcher.

`run_analysis_tasks` caps each analysis task's own timeout to the remaining
window (`ceil` seconds, minimum 1s) immediately before dispatch. An already
expired window therefore cannot start a full configured timeout, and a running
agent call is bounded near the cutoff by its existing hard-timeout kill.
Queueing is not exempt: `run_parallel(..., admit=...)` re-checks the window in
the worker when a queued task starts, and the same admission is evaluated again
at the TRUE pre-spawn boundary inside `run_task` — after worktree/marker
preparation and after the provider-rate wait, immediately before the agent
process is spawned. `spawn_registered` also receives the admission predicate
and runs it under the same lock as `Popen`, so a worker that waited behind
another worker's process creation re-evaluates the cutoff once the lock is
held; a refusal raises before any process is created and the runner returns
the refusal result. A refused task returns a failed `BUDGET_BLOCKED`-class
result without spawning. The provider-rate wait itself is bounded by the
remaining window (still capped by the historical 300s ceiling) so a token wait
cannot outlive the cutoff and then spawn; `admit.rate_timeout()` supplies that
bound. Retry sleeps are clamped to the remaining window and a retry is never
dispatched after expiry; the unchanged-failure dedup and provider-failure skip
logic still apply. If the queue is empty nothing changes.

Observing a nonpositive remaining window latches exhaustion even when it
appears between the wall-clock samples inside `remaining()`/`expired()`, so a
later wall rollback cannot reopen admission or re-permit a refused queued
task. A zero/negative window is itself a refusal: elapsed-time credit is only
added after the remaining window is positively observed, and the final
spawn-lock gate carries that refusal through `SpawnRefused` without creating a
process.

A task whose result is missing or empty while the deadline is expired is
recorded as `BUDGET_BLOCKED` instead of an ordinary `ANALYSIS_FAILED`, is not
counted as an ordinary analysis failure, and does not enter the "repeated
failure" suppression path. If analysis had to skip or cut any module,
`run_campaign` records `CAMPAIGN_BUDGET_EXHAUSTED` (reason: during analysis),
writes the summary, and returns terminal exit 3 before selection,
implementation, recovery or round handling. Analyses that completed
successfully before the cutoff remain reusable; a fully completed analysis
phase does not set the stop flag, so normal selection continues and the
existing direction/pipeline admission still decides.

## Limits

- This is admission plus a per-call timeout bound, not preemption of an
  already-running model request: the agent child is killed by its own bounded
  hard timeout near the cutoff, and provider-side work is not cancelled.
- Once the agent process has spawned, only the bounded hard timeout stops it;
  worktree/marker preparation, rate waiting and process-creation lock waiting
  before that boundary are checked at the boundary but not preemptible during
  the wait itself.
- Local filesystem/tree-digest work in the coordinator is charged through the
  wall/monotonic checks but cannot be forcibly preempted.
- Serial in-tree implementation (and the legacy standalone runtime helper) is
  outside this batch; it still relies on its task timeouts plus the shared
  admission checks.
- If a process crashes before freezing, a restart with `budget_seconds` and no
  ledger origin still anchors a fresh window for that run; the ledger-anchored
  origin remains the restart-proof path.
- Exit-3 handling, receipts, and rollback semantics are unchanged from the
  isolated-attempt contract in
  [CAMPAIGN_ISOLATED_DEADLINE.md](CAMPAIGN_ISOLATED_DEADLINE.md).

Offline tests cover expired admission, mid-preparation expiry, timeout caps,
retry stopping, `BUDGET_BLOCKED` classification, still-analyzed success, one
shared latch across phases under wall rollback, and terminal exit 3. They are
not provider, production-campaign, or complete-CI evidence.
