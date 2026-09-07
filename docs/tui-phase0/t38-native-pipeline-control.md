# T38 · Native Pipeline control boundary

Native Pipeline execution now has two explicitly separate capabilities:

- `NativePipelineAttachment` remains a read-only `RuntimeEvent` sink;
- `NativePipelineControlChannel` is an opt-in control capability passed to
  `run_pipeline(..., approval_channel=...)`.

The channel is bound once to the observer's exact
`workspace_id/session_id/run_id/turn_id/actor_id` tuple. A decision is accepted
only when all five fields, the request id and `approval_type` match the pending
request. Request ids retain their scalar type. The channel is bounded to one
pending approval and a finite TTL; timeout, attachment failure, or an invalid
scope fails closed rather than approving the stage.

An approval stage emits typed `approval.requested`, `approval.resolved`, or
`approval.expired` events. The TUI routes the request to the existing typed
approval card. Native Pipeline approval deliberately offers `allow`, `deny`,
and `cancel`; it does not pretend that `allow_session` has session-wide
semantics. A `cancel` decision is an exact-scope Pipeline cancellation.

`Ctrl+C` uses the same control capability and exact runtime scope. It emits a
trusted `turn.interrupting` fact and sets a scoped cancellation flag. The runner passes
that flag to the active local agent/validator process group, so the current
native task is killed promptly; queued tasks and later stages are not spawned.
Stage-boundary checks cover work that is between subprocesses. The final state
is `INTERRUPTED` and archive-on-success is skipped. Task, validation, stage, and
actor lifecycle facts carry bounded cancellation state (`cancelled`/`CANCELLED`)
when the runner observes the cooperative stop; the projection does not relabel
those facts as a provider failure. It does not call a provider stop API or kill
an unrelated provider turn. Detaching an attachment
remains presentation-only and never sends a cancellation request.

While a Pipeline is running, `/actor-stop <task-actor-id>` uses the same exact
root scope but a separately registered live task actor. It sets only that
local task's cancellation event; other parallel tasks and the Pipeline turn are
not cancelled. The task is unregistered as soon as its result is observed
(and again during stage cleanup), so stale actor ids fail closed. Native task
steering is intentionally unsupported because
there is no provider turn to steer.

Without an explicitly bound channel, native approval still fails closed in the
TUI; headless callers retain `--approve-file`, `PBATCH_APPROVE=1`, or a real
TTY. Control events do not become authority merely because they appear in a
transcript: the running Pipeline and the exact channel binding remain the
execution authority.
