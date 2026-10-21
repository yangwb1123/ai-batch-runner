# Campaign isolated-attempt deadline — second implementation batch

This extends the first batch's direction **admission** checks. It does not
start/resume a campaign, extend a budget, merge candidates, or certify ForgeOS.
The first batch's frozen source/review receipts remain a separate identity.

## Execution boundary

`run_directions_isolated` resolves one cutoff from the coordinator settings and
ledger and passes a copied settings object with an absolute cutoff to every
worktree. It does not modify caller settings or reread a worktree's private
ledger. The batch shares one immutable epoch/monotonic cap and a one-way
exhaustion latch across queued attempts and parent checks; wall rollback cannot
reauthorize queued work after a refusal. A private runtime-only settings copy
carries this state; original serialized settings/fingerprints are not mutated.
Existing absolute-deadline precedence over relative budgets is retained;
relative budgets still use the first recorded campaign start. The existing
missing-ledger fallback is frozen once for this batch, not reset per direction.
Deleting the authority ledger is not a supported budget recovery mechanism.

The isolated executor refuses an already expired/invalid/non-finite cutoff
before spawn, and rechecks after environment preparation. Passing an unresolved
relative budget directly to this executor is an error, not permission to start
a new budget in a worktree. The batch cap is captured before worktree preparation;
the per-pipeline timeout clock starts before environment/spawn/lease setup.
Wall rollback cannot extend this live batch; an observed wall advance may expire
it earlier. Parent checks preserve this cap rather than constructing fresh ones.

For a configured campaign cutoff, an independent owned alarm enforces the
earlier of the pipeline timeout and campaign cutoff. It is armed after the
new session leader's owned handle/local start identity is captured, before
lease setup. Lease/artifact I/O and continuing output cannot reset or block
that alarm. The normal watchdog still observes progress and returns ordinary
pipeline timeouts separately. No-progress duration begins at the first artifact
observation, not at an unobserved spawn/lease interval; this never resets the
early hard-timeout clock. Invalid cutoff configuration fails closed.

This is an OS-scheduled watchdog, **not a real-time guarantee**. Process creation,
local handle setup and coordinator filesystem/Git operations cannot be forcibly
preempted by Python. A blocked coordinator may return later even after the alarm
has stopped its child. Parent-side finalization has an additional cutoff check
before it starts, but Git/filesystem calls already in progress are not bounded
by this campaign alarm. The parent rechecks after Git evidence collection and
immediately before it starts finalization, not only before potentially slow Git
reads. It also checks after finalization/artifact I/O, before recovery, and before
returning the outcome. A common worker-return boundary also covers early
recovery-limit/history refusals, not just the post-child path. I/O admitted
earlier can finish late (including a commit);
that is not rollback, and late completion is not published as ordinary PASS.
Late parent/preparation errors also preserve budget termination. When an
interrupted parent operation has no safely available receipt references, report
that receipt unavailable rather than inventing paths or successful metadata.

## Ownership and stop semantics

A new isolated child is a session/process-group leader. Its handle serializes
polling, group signals and reaping. Zero-grace TERM/KILL is sent before reaping
the leader; an observer cannot reap/reuse that PID between signals. A previously
reaped handle is never reused as authority to signal its old PID. A deadline
monitor is required for a budgeted attempt; startup/monitor failure preserves
the worktree rather than silently falling back to an unguarded successful run.

The fresh handle is registered before fallible identity acquisition, and an
unknown child survives that failure in the active-owner registry. If a setup
interruption cannot confirm termination, it becomes a preserved supervision
failure, not an ordinary retry. Optional Git diagnostics are collected after
classifying the refusal, with another deadline check after diagnostic I/O even
on errors; diagnostic failure reports evidence unavailable and cannot erase that
refusal, authorize ordinary retry after cutoff, or fabricate a clean commit.

This is not an OS sandbox. It does not contain descendants that escape the
owned process group, cancel provider-side requests, undo writes or roll back
external effects. It does not prove the absence of background work after a
normally completed leader. If direct-child termination is not observed, retain
the live/unknown owner lease and tracked handle; do not publish a fake exit or
allow ordinary automatic restoration/retry. The active registry is process-local;
a lease that was never bound during setup cannot be claimed as durably retained.
This increment does not establish cross-crash/orphan containment or a durable
pre-spawn reservation, nor repair the whole signal-supervisor resume protocol.
Manual inspection is still required for unknown ownership/effects. Process
identity metadata is diagnostic, not remote attestation or execution authority.

## Outcomes and evidence

- Campaign expiry becomes `BUDGET_BLOCKED`, with `PRESERVED` recovery state and
  measured attempt elapsed time. A latched refusal survives lease/heartbeat or
  cleanup errors; receipts retain observed exception types rather than claiming
  those errors never occurred. No parent finalize or automatic recovery runs.
  The first batch's terminal exit-3 handling stops the round loop.
- A required monitor failure is `PIPELINE_FAILED` plus `PRESERVED`, not PASS and
  not an invented budget-expiry event. Existing recovery-block handling applies.
- An ordinary pipeline timeout remains a pipeline failure when termination is
  observed; callers without a campaign cutoff retain the four-value child result
  tuple. A completed-child observation is accepted only after checking both
  time limits, including time consumed inside poll and earlier setup. Unobserved
  termination, including ordinary timeout/stall paths, instead
  raises a required-supervision failure with `PRESERVED`, never ordinary recovery.
- Receipt creation failure cannot turn budget expiry into a retryable ordinary
  failure or an empty successful receipt. The refusal reports evidence missing.

Exclusive 0600 metadata receipts use:

```
<data_dir>/campaign/deadline-receipts/<direction>-<nonce>.json
```

They carry cutoff/observation clocks, phase, worktree/pipeline/log references,
observer PID, and available child PID/start-ticks/return code. Start ticks are
captured while the original child PID is still held, not reprobed after reaping.
Unknown child fields remain null. `group_stop_requested` is a request, **not**
proof that every descendant stopped; `effects_rolled_back` is always false.
Receipts use the existing 16KiB bounded exclusive writer. There is no claim of
directory-fsync/power-loss durability or complete artifact/process preservation.

## Still separate work

Coordinator-side analysis and serial in-tree implementation are not covered by
this isolated-child alarm. Neither is the legacy standalone runtime helper that
has no coordinator-budget input. Default isolation independent of concurrency,
unique run/branch namespaces, model/environment normalization, immutable shared
analysis handoff, and explicit stage write scopes remain separate increments.
Normal validation/acceptance thresholds, stage/role routing, and auto-merge
policy are unchanged. Tool-code changes naturally invalidate fingerprints; do
not forge old analysis reuse to hide that change.

Tests are offline controlled clocks, owned native subprocesses/process groups,
and private Git worktrees. Linux identity fixtures are Linux-only. They are not
provider authentication, remote execution, a production campaign rehearsal, or
proof of overall repository CI success.
