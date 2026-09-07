# T33 · Native Pipeline observer

`pbatch.pipeline.run_pipeline(..., event_sink=...)` now exposes an opt-in,
read-only native observation path. `NativePipelineObserver` emits a single
bounded RuntimeEvent stream for:

- pipeline and turn start/terminal boundaries;
- stage start/completion plus stage actor spawn/completion;
- task start/completion plus task actor spawn/completion;
- meta-stage role actors, each parented to the active stage;
- validation start/completion bound to the task actor;
- a deterministic-preflight `plan.updated` record containing only explicit
  pipeline scopes, commands, validators, gates, approval points, and budgets;
  the observer adds a bounded `plan_id`/`plan_digest` identity and carries it in
  later event extensions as evidence binding, never as an execution grant;
- gate verdicts;
- bounded `pipeline.checkpoint` stage-boundary facts carrying completed/next stage
  indices, task counts and `next_stage`/`retry_stage` diagnostic resume boundary.
  The TUI exposes the latest typed boundary as `view.pipeline_checkpoint`; these
  facts are recovery evidence only and never authorize replay or execution.

The existing execution and gate code remains authoritative. The observer is
attached through a context-local bridge, so ordinary pipeline calls have no new
state or output. A presentation sink failure disables observation and never
changes a task, stage, gate, or pipeline result. Dry runs do not emit successful
execution facts.

The generated event identities are scoped to one pipeline invocation and use a
root `pipeline` actor. Each stage is a typed child actor of that root, each
task is a typed child actor of its stage, and meta role actors are typed stage
children; lifecycle envelopes carry the exact
`actor_id` and `parent_actor_id` used by the Actor Tree validator. All fields are
bounded; task prompts are not persisted, and task output paths are bounded
references. The TUI controller projects the new pipeline/stage/task entries
alongside plan, validator/gate/actor entries, while journal durability and recovery
continue using the existing RuntimeEvent contract. The preflight plan is display evidence
only; it does not grant or widen execution authority.

The TUI also exposes explicit `app.attach_native_pipeline(workspace_id)`;
passing `session_id=...` recovers the exact native journal before new events arrive.
Pass
the returned callable as `run_pipeline(event_sink=...)` to project the same
read-only lifecycle into TUI Timeline/Completion/Actor views. The attachment
starts with a separate controller and accepts only the exact workspace plus
`pbatch-native`/`read_only` markers; for an existing workspace path it lazily
binds the first exact native session to its own scoped journal and checkpoint,
so later attach/replay can validate the same observation history. A missing or
disabled journal falls back to the bounded in-memory controller; invalid input
closes it and lets the observer's sink-failure path disable observation.
Detaching never sends stop or cancel. The TUI now starts explicit Pipelines through `/pipeline <file>` or
`tui --pipeline FILE`; approval stages use a separate opt-in, exact-run-scoped
`NativePipelineControlChannel` and emit typed request/resolution/expiry facts.
While running, `/actor-stop` may cancel one registered local task actor with
exact root scope; `/actor-steer` remains unsupported for native tasks. Whole-
pipeline provider stop/cancel remains intentionally unintroduced.
